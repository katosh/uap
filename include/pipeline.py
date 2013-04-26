import copy
import csv
import datetime
import glob
import json
import os
import re
import StringIO
import subprocess
import sys
import yaml

import abstract_step
import task as task_module

# an enum class, yanked from http://stackoverflow.com/questions/36932/whats-the-best-way-to-implement-an-enum-in-python
class Enum(set):
    def __getattr__(self, name):
        if name in self:
            return name
        raise AttributeError

# an exception class for reporting configuration errors
class ConfigurationException(Exception):
    def __init__(self, value):
        self.value = value
    def __str__(self):
        return repr(self.value)

class Pipeline(object):
    
    '''
    The Pipeline class represents the entire processing pipeline which is defined and 
    configured via the configuration file config.yaml.
    
    Individual steps may be defined in a tree, and their combination with samples
    as generated by one or more source leads to an array of tasks.
    '''

    states = Enum(['WAITING', 'READY', 'FINISHED'])

    def __init__(self):
        
        # now determine the Git hash of the repository
        self.git_hash_tag = subprocess.check_output(['git', 'describe', '--all', '--dirty', '--long']).strip()
        if '-dirty' in self.git_hash_tag:
            if not '--even-if-dirty' in sys.argv:
                print("The repository has uncommitted changes, which is why we will exit right now.")
                print("If this is not a production environment, you can skip this test by specifying --even-if-dirty on the command line.")
                exit(1)

        if '--even-if-dirty' in sys.argv:
            sys.argv.remove('--even-if-dirty')
            
        # the configuration as read from config.yaml
        self.config = {}

        # dictionary of sample names => information
        self.all_samples = {}

        # dict of steps, steps are objects with inter-dependencies
        self.steps = {}

        self.read_config()

        # collect all tasks
        self.task_for_task_id = {}
        self.all_tasks = []
        for step in self.steps.values():
            if not abstract_step.AbstractSourceStep in step.__class__.__bases__:
                for run_id in sorted(step.get_run_ids()):
                    task = task_module.Task(self, step, run_id)
                    self.all_tasks.append(task)
                    if str(task) in self.task_for_task_id:
                        raise ConfigurationException("Duplicate task ID %s. Use the 'step_name' option to assign another step name." % str(task))
                    self.task_for_task_id[str(task)] = task

        self.tool_versions = {}
        self.check_tools()

    # read configuration and make sure it's good
    def read_config(self):
        #print >> sys.stderr, "Reading configuration..."
        self.config = yaml.load(open('config.yaml'))
        '''
        if not 'sources' in self.config:
            raise ConfigurationException("Missing key: sources")
        for source in self.config['sources']:
            key = source.keys()[0]
            source_instance = abstract_source.get_source_class_for_key(key)(self, source[key])
            for sample_id, sample_info in source_instance.samples.items():
                if sample_id in self.all_samples:
                    raise ConfigurationException("Sample appears multiple times in sources: " + sample_id)
                self.all_samples[sample_id] = copy.deepcopy(sample_info)
                '''
        if not 'destination_path' in self.config:
            raise ConfigurationException("Missing key: destination_path")
        if not os.path.exists(self.config['destination_path']):
            raise ConfigurationException("Destination path does not exist: " + self.config['destination_path'])

        if not os.path.exists("out"):
            os.symlink(self.config['destination_path'], 'out')

        self.build_steps()

    def build_steps(self):
        self.steps = {}
        if not 'steps' in self.config:
            raise ConfigurationException("Missing key: steps")

        # step one: instantiate all steps
        for step_id, step_description in self.config['steps'].items():
            step_name = step_id
            if '_step' in step_description:
                step_name = step_description['_step']
                
            step_class = abstract_step.get_step_class_for_key(step_name)
            step = step_class(self)
            
            step.set_name(step_id)
            step.set_options(step_description)
            
            self.steps[step_id] = step
            
        # step two: set dependencies
        for step_id, step_description in self.config['steps'].items():
            if not '_depends' in step_description:
                raise ConfigurationException("Missing key in step '%s': "
                    "_depends (set to null if the step has no dependencies)." 
                    % step_id)
            depends = step_description['_depends']
            if depends == None:
                pass
            else:
                temp_list = depends
                if depends.__class__ == str:
                    temp_list = [depends]
                for d in temp_list:
                    if not d in self.steps:
                        raise ConfigurationException("Unknown dependency: %s." % d)
                    self.steps[step_id].add_dependency(self.steps[d])
                    

    def print_tasks(self):
        '''
        prints a summary of all tasks, indicating whether each taks is
          - ``[r]eady``
          - ``[w]aiting``
          - ``[f]inished``
        '''
        count = {}
        for task in self.all_tasks:
            state = task.get_task_state()
            if not state in count:
                count[state] = 0
            count[state] += 1
            print('[' + task.get_task_state()[0].lower() + '] ' + str(task))
        print('tasks: ' + str(len(self.all_tasks)) + ' total, ' + ', '.join([str(count[_]) + ' ' + _.lower() for _ in sorted(count.keys())]))

    def has_unfinished_tasks(self, task_list):
        '''
        returns True if there is at least one task in task_list which is not finished yet
        '''
        unfinished_tasks = [task for task in task_list if task.step.get_run_state(task.run_id) != self.states.FINISHED]
        return len(unfinished_tasks) > 0

    def pick_next_ready_task(self, task_list):
        '''
        returns the next ready task from task_list but does not remove it from the list
        '''
        ready_tasks = [task for task in task_list if task.step.get_run_state(task.run_id) == self.states.READY]
        return ready_tasks[0]

    def check_tools(self):
        '''
        checks whether all tools references by the configuration are available 
        and records their versions as determined by ``[tool] --version`` etc.
        '''
        if not 'tools' in self.config:
            return
        for tool_id, info in self.config['tools'].items():
            command = [info['path']]
            if 'get_version' in info:
                command.append(info['get_version'])
            exit_code = None
            try:
                proc = subprocess.Popen(command, stdout = subprocess.PIPE,
                    stderr = subprocess.PIPE, close_fds = True)
            except:
                raise ConfigurationException("Tool not found: " + info['path'])
            proc.wait()
            exit_code = proc.returncode
            self.tool_versions[tool_id] = {
                'command': (' '.join(command)).strip(),
                'exit_code': exit_code,
                'response': (proc.stdout.read() + proc.stderr.read()).strip()
            }
            expected_exit_code = 0
            if 'exit_code' in info:
                expected_exit_code = info['exit_code']
            if exit_code != expected_exit_code:
                raise ConfigurationException("Tool check failed for " + tool_id + ": " + ' '.join(command) + ' - exit code is: ' + str(exit_code) + ' (expected ' + str(expected_exit_code) + ')')

    # returns a short description of the configured pipeline
    def __str__(self):
        s = ''
        s += "Number of samples: " + str(len(self.all_samples)) + "\n"
        for sample in sorted(self.all_samples.keys()):
            s += "- " + sample + "\n"
        return s

    def notify(self, message):
        '''
        prints a notification to the screen and optionally delivers the
        message on additional channels (as defined by the configuration)
        '''
        print(message)
        if 'notify' in self.config:
            try:
                notify = self.config['notify']
                match = re.search('^(http://[a-z\.]+:\d+)/([a-z0-9]+)$', notify)
                if match:
                    host = match.group(1)
                    token = match.group(2)
                    args = ['curl', host, '-X', 'POST', '-d', '@-']
                    proc = subprocess.Popen(args, stdin = subprocess.PIPE)
                    proc.stdin.write(json.dumps({'token': token, 'message': message}))
                    proc.stdin.close()
                    proc.wait()
            except:
                # swallow all exception that happen here, failing notifications
                # are no reason to crash the entire thing
                pass
