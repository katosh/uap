'''
Classes AbstractStep and AbstractSourceStep are defined here.

The class AbstractStep has to be inherited by all processing step classes.
The class AbstractSourceStep has to be inherited by all source step classes.

Processing steps generate output files from input files whereas source steps
only provide output files. Both step types may generates tasks, but only source
steps can introduce files from outside the destination path into the pipeline.
'''

# 1. standard library imports
import sys
sys.path.insert(0, './include/steps')
sys.path.insert(0, './include/sources')
import copy
import datetime
import inspect
import json
import logging
import os
import re
import random
import signal
import socket
import string
import StringIO
import subprocess
import tempfile
import textwrap
import time
import traceback
# 2. related third party imports
import fscache
import psutil
import yaml
# 3. local application/library specific imports
import command as command_info
import misc
import process_pool
import pipeline_info
import run as run_module

logger = logging.getLogger('uap_logger')

class AbstractStep(object):
    
    fsc = fscache.FSCache()
    
    PING_TIMEOUT = 300
    PING_RENEW = 30
    VOLATILE_SUFFIX = '.volatile.placeholder.yaml'
    UNDERSCORE_OPTIONS = ['_depends', '_volatile', '_BREAK', '_connect']
    
    states = misc.Enum(['DEFAULT', 'DECLARING', 'EXECUTING'])

    def __init__(self, pipeline):
        
        self._pipeline = pipeline
        
        self.dependencies = []
        '''
        All steps this step depends on.
        '''
        
        self._options = {}
        '''
        Options as specified in the configuration.
        '''
        
        self._step_name = self.__module__
        '''
        By default, this is the name of the module. Can be overridden 
        to allow for multiple steps of the same kind.
        '''
        
        self._runs = None
        '''
        Cached run information. ``declare_runs`` is only called once, the
        post-processed run objects are stored in here.
        '''
        
        self._temp_directory = None
        '''
        The temporary output directory the step is using. Only set when
        the step is being run.
        '''
        
        self._pipeline_log = dict()
        
        self._cores = 1
        self._connections = set()
        self._connection_restrictions = {}
        self._pre_command = dict()
        self._post_command = dict()
        self._module_load = dict()
        self._module_unload = dict()
        self._tools = dict()

        self._defined_options = dict()
        
        self.needs_parents = False
        
        self.known_paths = dict()
        
        self.children_step_names = set()
        
        self.finalized = False
        
        self._state = AbstractStep.states.DEFAULT

    def finalize(self):
        '''Finalizes the step.
        
        The intention is to make further changes to the step
        impossible, but apparently, it's checked nowhere at the moment.
        '''
        if self.finalized:
            return
        
        for parent_step in self.dependencies:
            parent_step.finalize()
            
        self.finalized = True
        
    def _reset(self):
        self.known_paths = dict()
        self._pipeline_log = dict()

    def get_pipeline(self):
        return self._pipeline

    def declare_run(self, run_id):
        '''
        Declare a run. Use it like this::
        
            with self.declare_run(run_id) as run:
                # add output files and information to the run here
        '''
        if run_id in self._runs:
            raise StandardError(
                "Cannot declare the same run ID twice: %s." % run_id)
        run = run_module.Run(self, run_id)
        self.add_run(run)
        return run

    def add_run(self, run):
        self._runs[run.get_run_id()] = run
    
    def get_run(self, run_id):
        '''
        Returns a single run object for run_id or None.
        '''
        if run_id in self._runs:
            return self._runs[run_id]
        else:
            return None

    def get_runs(self):
        '''
        Returns all run objects of the step.
        '''
        for run in self._runs:
            yield self._runs[run]

    def get_output_directory_du_jour_placeholder(self):
        '''
        Returns a placeholder for the temporary output directory, which
        needs to be replaced by the actual temp directory inside the
        execute() method
        '''
        return("<%s-output-directory-du-jour>" %  
               str(self.__class__.__name__))

    def set_step_name(self, step_name):
        '''
        Change the step name.

        The step name is initially set to the module name. This method
        is used in case we need multiple steps of the same kind.
        '''
        self._step_name = step_name

    def get_step_name(self):
        '''
        Return the steps name
        '''
        return self._step_name

    def set_options(self, options):
        '''
        Checks and stores step options.

        The options are either set to values given in YAML config or
        the default values set in self.add_option().
        '''
        self._options = dict()
        
        # set options
        for key, value in options.items():
            if key[0] == '_':
                if not key in AbstractStep.UNDERSCORE_OPTIONS:
                    raise StandardError(
                        "Invalid option in %s: %s" % (key, self))
                self._options[key] = value
            else:
                if not key in self._defined_options:
                    raise StandardError(
                        "Unknown option in %s (%s): %s." % 
                        (self._step_name, self.__module__, key))
                if type(value) not in self._defined_options[key]['types']:
                    raise StandardError(
                        "Invalid type for option %s - it's %s and should be "
                        "one of %s." % (key, type(value), 
                                        self._defined_options[key]['types']))
                if self._defined_options[key]['choices'] != None and \
                   value not in self._defined_options[key]['choices']:
                    raise StandardError(
                        "Invalid value '%s' specified for option %s - "
                        "possible values are %s." % 
                        (value, key, self._defined_options[key]['choices']))
                self._options[key] = value

        # set default values for unset options and make sure all required 
        # options have been set        
        for key, info in self._defined_options.items():
            if key not in self._options:
                value = info['default']
                if value == None:
                    if info['optional'] == False:
                        raise StandardError(
                            "Required option not set in %s: %s." % (self, key))
                else:
                    self._options[key] = value
                
        if not '_volatile' in self._options:
            self._options['_volatile'] = False
            
    def add_dependency(self, parent):
        '''
        Add a parent step to this steps dependencies.

        parent -- parent step this step depends on
        '''
        if not isinstance(parent, AbstractStep):
            raise StandardError(
                "Error: parent argument must be an AbstractStep.")
        if parent == self:
            raise StandardError("Cannot add a node as its own dependency.")
        self.dependencies.append(parent)
        parent.children_step_names.add(str(self))
        
    def which_extensions_match_file_path(self, filepath, extensions):
        # Kann evtl. auch weg!
        extension_list = list()
        if type(filepath) is not str:
            raise StandardError("Filename must be string. Got %s of type %s"
                                % (filepath, type(filepath)) )
        for ext in extensions:
            if type(ext) is not str:
                raise StandardError("Found non-string file extension: %s "
                                    % ext)
            else:
                extension_list.append(ext)
        
        ext_in_filename = list()
        file_parts = os.path.basename(filepath).split(".")
        for ext in extension_list:
            if ext in file_parts:
                ext_in_filename.append(ext)
        return ext_in_filename

    def get_input_run_info(self):
        '''
        Return a dict with run info for each parent.
        '''
        input_run_info = dict()
        for parent in self.dependencies:
            input_run_info[parent.get_step_name()] = parent.get_runs()
        return input_run_info

    def declare_runs(self):
        # Was muss hier alles passieren damit es funktioniert?
        # * es muessen alle runs definiert werden
        # * pro run muessen alle public/private Infos gesetzt werden
        # * es MUESSEN die Output Dateien den Connections zugeordnet werden

        # fetch all incoming run IDs which produce reads...

        in_connections = self.get_in_connections()
        run_ids_connections_files = dict()
        for in_connection in in_connections:
            for run_id, input_paths in self.get_run_ids_and_input_files_for_connection(in_connection):
                # das macht den schoenen Generator kaputt den Micha mal gebaut hat
                if not run_id in run_ids_connections_files:
                    run_ids_connections_files[run_id] = dict()
                if not in_connection in run_ids_connections_files[run_id]:
                    run_ids_connections_files[run_id][in_connection] = input_paths
        
        self.runs(run_ids_connections_files)
        
    def runs(self, run_ids_connections_files):
        '''
        Abstract method this must be implemented by actual step.

        Raise NotImplementedError if subclass does not override this
        method.
        '''
        raise NotImplementedError()
        
    def execute(self, run_id, run):
        # Ich muss noch ne Loesung finden um hier beliebigen Python Code auszufuehren
        # exec() oder eval()?

        # get run_info objects
        with self.get_run(run_id) as run:
            print("Run ID: %s" % run_id)
            # for each exec_group in that run ...
            for exec_group in run.get_exec_groups():
                # ... create a process pool
                with process_pool.ProcessPool(run) as pool:
                    # Clean up (use last ProcessPool for that)
                    if exec_group == run.get_exec_groups()[-1]:
                        pool.clean_up_temp_paths()

                    for poc in exec_group.get_pipes_and_commands():
                        # for each pipe or command (poc)
                        # check if it is a pipeline ...
                        if isinstance(poc, pipeline_info.PipelineInfo):
                            # ... create a pipeline ...
                            with pool.Pipeline(pool) as pipeline:
                                for command in poc.get_commands():
                                    pipeline.append(
                                        command.get_command(),
                                        stdout_path = command.get_stdout_path(),
                                        stderr_path = command.get_stderr_path())
                        elif isinstance(poc, command_info.CommandInfo):
                            pool.launch(
                                poc.get_command(),
                                stdout_path = poc.get_stdout_path(),
                                stderr_path = poc.get_stderr_path())

    def get_runs(self):
        '''
        Getter method for runs of this step.

        If there are no runs as this method is called, they are created here.
        '''
        # create runs if they don't exist yet
        if not self._runs:
            # if _BREAK: true is specified in the configuration,
            # return no runs and thus cut off further processing
            if '_BREAK' in self._options and self._options['_BREAK']:
                return dict()
                
            self._runs = dict()
            
            self._state = AbstractStep.states.DECLARING
            self.declare_runs()
            self._state = AbstractStep.states.DEFAULT
            
            # define file dependencies
            for run_id in self._runs.keys():
                for annotation in self.get_run(run_id).get_output_files_abspath().keys():
                    for output_path, input_paths in \
                        self.get_run(run_id).get_output_files_abspath()[annotation].items():
                        # proceed if we have normal output_path/input_paths
                        if output_path != None and input_paths != None:
                            # store file dependencies
                            self.get_pipeline().add_file_dependencies(
                                output_path, input_paths)
                            # create task ID
                            task_id = '%s/%s' % (str(self), run_id)
                            self.get_pipeline().add_task_for_output_file(
                                output_path, task_id)
                            # No input paths? Add empty string NOT None
                            # as file name
                            if len(input_paths) == 0:
                                self.get_pipeline().add_task_for_input_file(
                                    "", task_id)
                            for input_path in input_paths:
                                self.get_pipeline().add_task_for_input_file(
                                    input_path, task_id)

        # now that _runs exists, it remains constant, just return it
        return self._runs

    def get_run_ids(self):
        '''
        Returns sorted list of runs generated by step.
        '''
        return sorted(self.get_runs().keys())

    def get_options_hashtag(self):
        '''
        Creates a hash tag for the options given.

        
        This causes steps to be marked for rerunning if the options inside
        the config file are changed.
        '''
        options_without_dash_prefix = dict()
        for k, v in self._options.items():
            if k[0] != '_':
                options_without_dash_prefix[k] = v
        return misc.str_to_sha1(json.dumps(options_without_dash_prefix, 
                                           sort_keys=True))[0:4]

    def get_step_name(self):
        '''
        Returns this steps name.

        Returns the step name which is initially equal to the step type 
        (== module name)  but can be changed via set_step_name() or via
        the YAML configuration.
        '''
        return self._step_name

    def get_step_type(self):
        '''
        Returns the original step name (== module name).
        '''
        return self.__module__

    def get_output_directory(self):
        '''
        Returns the final output directory path.
        '''
        return os.path.join(self.get_pipeline().config['destination_path'], 
            '%s-%s' % (self.get_step_name(), self.get_options_hashtag()))

    def get_output_directory_du_jour(self, run_id):
        '''
        Returns the state-dependent output directory of the step.


        Returns this steps output directory according to its current
        state:
            - if we are currently calling a step's declare_runs()
              method, this will return None
            - if we are currently calling a step's execute() method,
              this will return the temporary directory
            - otherwise, it will return the real output directory
        '''
        if self._state == AbstractStep.states.DEFAULT:
            return self.get_output_directory()
        elif self._state == AbstractSourceStep.states.EXECUTING:
            return self.get_run(run_id).get_temp_output_directory()
        else:
            return None

    def get_temp_output_directory(self, run_id):
        '''
        Returns the temporary output directory of a step.
        '''
        if self._temp_directory == None:
            while True:
                token = ''.join(random.choice(
                    string.ascii_lowercase + string.digits) for x in range(8))
                path = os.path.join(
                    self.get_pipeline().config['destination_path'],
                    'temp', 'temp-%s-%s-%s' % (str(self), run_id, token))
                if not os.path.exists(path):
                    self._temp_directory = path
        return self._temp_directory

    def get_run_state_basic(self, run_id):
        '''
        Determines basic run state of a run.

        Determine the basic run state of a run, which is, at any time, one of
        **waiting**, **ready**, or **finished**.
        
        These states are determined from the current configuration and the
        timestamps of result files present in the file system. In addition to
        these three basic states, there are two additional states which are
        less reliable (see *get_run_state()*).
        '''
        
        def volatile_path_good(volatile_path, recurse = True):
            '''
            This function receives a volatile path and tries to load the
            placeholder YAML data structure. It then checks all downstream
            paths, which may in turn be volatile placeholder files.
            '''
            
            # reconstruct original path from volatile placeholder path
            path = volatile_path[:-len(AbstractStep.VOLATILE_SUFFIX)]
            
            if AbstractStep.fsc.exists(path):
                # the original file still exists, ignore volatile placeholder
                return False
            
            if not path in self.get_pipeline().task_id_for_output_file:
                # there is no task which creates the output file
                return False
            
            task_id = self.get_pipeline().task_id_for_output_file[path]
            
            task = self.get_pipeline().task_for_task_id[task_id]
#            if not task.step.options['_volatile']:
            if not task.step._options['_volatile']:
                # the task is not declared volatile
                return False
            
            if not AbstractStep.fsc.exists(volatile_path):
                # the volatile placeholder does not exist
                return False
            
            if not recurse:
                return True
            
            try:
                # try to parse the YAML contents
                info = AbstractStep.fsc.load_yaml_from_file(volatile_path)
            except yaml.scanner.ScannerError:
                # error scanning YAML
                return False
            
            # now check whether all downstream files are in place and up-to-date
            # also check whether all downstream files as defined in
            # file_dependencies_reverse are covered

            uncovered_files = set()
            if path in self.get_pipeline().file_dependencies_reverse:
                uncovered_files = self.get_pipeline().\
                                  file_dependencies_reverse[path]
                
            for downstream_path, downstream_info in info['downstream'].items():
                if downstream_path in self.get_pipeline().task_id_for_output_file:
                    # only check this downstream file if there's a task which 
                    # creates it, otherwise, it may be a file which is no more
                    # used
                    pv_downstream_path = change_to_volatile_if_need_be(
                        downstream_path, recurse = False)
                    if not AbstractStep.fsc.exists(pv_downstream_path):
                        return False
                    if not AbstractStep.fsc.getmtime(pv_downstream_path) >= \
                       info['self']['mtime']:
                        return False
                    if downstream_path in uncovered_files:
                        uncovered_files.remove(downstream_path)
                
            if len(uncovered_files) > 0:
                # there are still files defined which are not covered by the
                # placeholder
                return False
                
            return True
        
        def change_to_volatile_if_need_be(path, recurse = True):
            """
            Changes the file path to volatile path if necessary."""
            if path != None:
                if not AbstractStep.fsc.exists(path):
                    # the real output file does not exist
                    volatile_path = path + AbstractStep.VOLATILE_SUFFIX
                    if volatile_path_good(volatile_path, recurse):
                        return volatile_path
                return path

        def is_path_up_to_date(outpath, inpaths):
            """Checks if

            First, replace paths with volatile paths if the step is marked
            as volatile and the real path is missing.
            But, only consider volatile placeholders if all child tasks
            are finished. That means if a child of a volatile
            step needs to be run because it has been added or an existing step
            has been modified, the volatile placeholders are ignored, thus
            turning the task from 'finished' to 'ready' or 'waiting'
            Hint: The pv_ prefix is for 'possibly volatile'
            """
            pv_outpath = outpath
            pv_inpaths = list()
            
            if outpath in self.get_pipeline().task_id_for_output_file:
                pv_outpath = change_to_volatile_if_need_be(outpath)
                
            for inpath in inpaths:
                pv_inpaths.append(change_to_volatile_if_need_be(inpath))
                
            if not AbstractStep.fsc.exists(pv_outpath):
                return False
            for pv_inpath in pv_inpaths:
                if not AbstractStep.fsc.exists(pv_inpath):
                    return False
                if AbstractStep.fsc.getmtime(pv_inpath) > \
                   AbstractStep.fsc.getmtime(pv_outpath):
                    return False
            return True
            
        def up_to_dateness_level(path, level = 0):
            result = level
            if path != None:
                dep_paths = self.get_pipeline().file_dependencies[path]
                if not is_path_up_to_date(path, dep_paths):
                    result = level + 1
                for dep_path in dep_paths:
                    recursive_result = up_to_dateness_level(dep_path, level + 1)
                    if recursive_result > level + 1:
                        result = max(result, recursive_result)
                return result

        """
        - finished: all output files exist AND up to date (recursively)
        - ready: NOT all output files exist AND all input files exist AND up to
                 date (recursively)
        - waiting: otherwise
        - if it's ready, it might be executing or queued -> check execute and
          queue ping
        - if it's waiting, it might be queued -> check queue ping
        
        the ping works like this (this example is for execute, same goes for 
        queued):
          - there's a ping file for every task ( = step + run)
          - it contains information about when, how, where the job was started
            etc.
          - its timestamp gets renewed every 30 seconds (touch)
          - as soon as the job has finished, the execute ping file is removed,
            this should also work if the job crashes (however, it cannot work
            if the controlling script receives SIGKILL
          - if its timestamp is no more than 5 minutes old, it is regarded as
            currently executing
          - otherwise, a warning is printed because the ping file is probably 
            stale (no automatic cleanup is performed, manual intervention is
            necessary)
          - warning: this requires all involved systems or the file system to
            be time-synchronized
        """
        
        run_info = self.get_runs()
        max_level = 0
        for tag, output_files in run_info[run_id].get_output_files_abspath().items():
            # output_files can be None if the connection is empty
            for output_file, input_files in output_files.items():
                if output_file != None and input_files != None:
                    max_level = max(
                        max_level, up_to_dateness_level(output_file))

        if max_level == 0:
            return self.get_pipeline().states.FINISHED
        elif max_level == 1:
            return self.get_pipeline().states.READY
        else:
            return self.get_pipeline().states.WAITING

    def get_run_state(self, run_id):
        '''
        Returns run state of a run.

        Determine the run state (that is, not *basic* but *extended* run state) 
        of a run, building on the value returned by *get_run_state_basic()*.
        
        If a run is **ready**, this will:
          - return **executing** if an up-to-date *executing ping file* is found
          - otherwise return **queued** if a *queued ping file* is found

        If a run is **waiting**, this will:
          - return **queued** if a *queued ping file* is found
          
        Otherwise, it will just return the value obtained from 
        *get_run_state_basic()*.
        
        Attention: The status indicators **executing** and **queued** may be 
        temporarily wrong due to the possiblity of having out-of-date ping files 
        lying around.
        '''
        run = self.get_run(run_id)
        run_state = self.get_run_state_basic(run_id)
        if run_state == self.get_pipeline().states.READY:
            if AbstractStep.fsc.exists( run.get_executing_ping_file() ):
                # here, we just check whether the executing ping file exists,
                # it doesn't matter whether it's been stale for a year
                # (the user will get notified that there are stale ping files
                # and can fix it with ./fix-problems.py, it's probably better
                # to fix this explicitly
                return self.get_pipeline().states.EXECUTING
            if AbstractStep.fsc.exists( run.get_queued_ping_file() ):
                return self.get_pipeline().states.QUEUED
        elif run_state == self.get_pipeline().states.WAITING:
            if AbstractStep.fsc.exists( run.get_queued_ping_file() ):
                return self.get_pipeline().states.QUEUED
        return run_state
        
    def run(self, run_id):
        '''
        Create a temporary output directory and execute a run. After the run
        has finished, it is checked that all output files are in place and
        the output files are moved to the final output location. Finally,
        YAML annotations are written.
        '''

        # this is the run we'll execute now
        run = self.get_run(run_id)

        # create the output directory if it doesn't exist yet
        if not os.path.isdir(self.get_output_directory()):
            os.makedirs(self.get_output_directory())
    
        # now write the run ping file
        executing_ping_path = run.get_executing_ping_file()
        
        if os.path.exists(executing_ping_path):
            raise StandardError("%s/%s seems to be already running, "
                                "exiting..." % (self, run_id))
        
        queued_ping_path = run.get_queued_ping_file()
        
        # create a temporary directory for the output files
        temp_directory = run.get_temp_output_directory()
        os.makedirs(temp_directory)

        # prepare self.known_paths
        self.known_paths = dict()
        for tag, tag_info in run.get_output_files_abspath().items():
            for output_path, input_paths in tag_info.items():
                # add the real output path
                if output_path != None and input_paths != None:
                    self.known_paths[output_path] = {
                        'type': 'output', 
                        'designation': 'output', 
                        'label': os.path.basename(output_path), 
                        'type': 'step_file'}
                    # ...and also add the temporary output path
                    self.known_paths[
                        os.path.join(temp_directory, os.path.basename(
                            output_path))] = {
                        'type': 'output', 
                        'designation': 'output', 
                        'label': "%s\\n(%s)" % 
                                (os.path.basename(output_path), tag), 
                        'type': 'step_file', 
                        'real_path': output_path}
                    for input_path in input_paths:
                        if input_path != None:
                            self.known_paths[input_path] = {
                                'type': 'input', 
                                'designation': 'input', 
                                'label': os.path.basename(input_path), 
                                'type': 'step_file'}

        # now write the run ping file
        executing_ping_info = dict()
        executing_ping_info['start_time'] = datetime.datetime.now()
        executing_ping_info['host'] = socket.gethostname()
        executing_ping_info['pid'] = os.getpid()
        executing_ping_info['cwd'] = os.getcwd()
        executing_ping_info['temp_directory'] = run.get_temp_output_directory()
        
        with open(executing_ping_path, 'w') as f:
            f.write(yaml.dump(executing_ping_info, default_flow_style = False))
            
        executing_ping_pid = os.fork()
        if executing_ping_pid == 0:
            try:
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                signal.signal(signal.SIGINT, signal.SIG_IGN)
                while True:
                    time.sleep(AbstractStep.PING_RENEW)
                    # if the executing ping file is gone and the touching
                    # operation fails, then SO BE IT!
                    os.utime(executing_ping_path, None)
            finally:
                os._exit(0)
            
        self.start_time = datetime.datetime.now()
        self.get_pipeline().notify(
            "[INFO] [%s] starting %s/%s on %s" % 
            (self.get_pipeline().config['id'], str(self), run_id, 
             socket.gethostname()))
        caught_exception = None
        self._state = AbstractStep.states.EXECUTING
        try:
            self.execute(run_id, run)
        except Exception as e:
            # Oh my. We have a situation. This is awkward. Tell the process
            # pool to wrap up. This way, we can try to get process stats before
            # shutting everything down.
            process_pool.ProcessPool.kill()
            # Store the exception, re-raise it later
            caught_exception = sys.exc_info()
        finally:
            try:
                os.kill(executing_ping_pid, signal.SIGTERM)
                os.waitpid(executing_ping_pid, 0)
            except OSError:
                # if the ping process was already killed, it's gone anyway
                pass
            # don't remove the ping file, rename it so we can inspect it later
            ping_file_suffix = misc.str_to_sha1_b62(
                run.get_temp_output_directory())[:6]
            if os.path.exists(executing_ping_path):
                try:
                    os.rename(executing_ping_path, 
                              executing_ping_path + '.' + ping_file_suffix)
                except OSError:
                    pass
            # remove the queued ping file
            if os.path.exists(queued_ping_path):
                try:
                    os.rename(queued_ping_path, 
                              queued_ping_path + '.' + ping_file_suffix)
                except OSError:
                    pass
                
        # TODO: Clean this up. Re-think exceptions and task state transisitions.
        
        self.end_time = datetime.datetime.now()
        
        if (not self.get_pipeline().caught_signal) and (caught_exception is None):
            # if we're here, we can assume the step has finished successfully
            # now rename the output files (move from temp directory to
            # destination directory)

            # import pdb
            # pdb.set_trace()

            for tag in run.get_output_files().keys():
                for out_file in run.get_output_files()[tag].keys():
                    # don't try to rename files if they were not meant to exist
                    # in our temporary directory
                    # 1. out_file should not be None (empty output connection)
                    # 2. out_file should not contain a '/' (file belongs to a
                    #    source step)
                    if out_file != None and not '/' in out_file:
                        source_path = os.path.join(
                            run.get_temp_output_directory(), 
                            os.path.basename(out_file)
                        )
                        destination_path = os.path.join(
                            self.get_output_directory(), 
                            os.path.basename(out_file))
                        # first, delete a possibly existing volatile placeholder
                        # file
                        destination_path_volatile = destination_path + \
                                                    AbstractStep.VOLATILE_SUFFIX
                        if os.path.exists(destination_path_volatile):
                            os.unlink(destination_path_volatile)
                        # TODO: if the destination path already exists, this 
                        # will overwrite the file.
                        if os.path.exists(source_path):
                            os.rename(source_path, destination_path)
                        else:
                            caught_exception = (
                                None, 
                                StandardError(
                                    "The step failed to produce an announced "
                                    "output file: %s." % 
                                    os.path.basename(out_file)), 
                                None)

        for path, path_info in self.known_paths.items():
            if os.path.exists(path):
                self.known_paths[path]['size'] = os.path.getsize(path)
                
        annotation_path, annotation_str = run.write_annotation_file(
            run_id, 
            self.get_output_directory() \
            if ((self.get_pipeline().caught_signal is None) and \
                (caught_exception is None)) \
            else run.get_temp_output_directory())

        self._state = AbstractStep.states.DEFAULT
#        self._temp_directory = None

        if self.get_pipeline().caught_signal is not None or \
           caught_exception is not None:
            message = "[BAD] %s/%s failed on %s after %s\n" % \
                      (str(self), run_id, socket.gethostname(), 
                       misc.duration_to_str(self.end_time - self.start_time))
            message += "\nHere are the details:\n" + annotation_str
            attachment = None
            if os.path.exists(annotation_path + '.png'):
                attachment = dict()
                attachment['name'] = 'details.png'
                attachment['data'] = open(annotation_path + '.png').read()
            self.get_pipeline().notify(message, attachment)
            if caught_exception is not None:
                raise caught_exception[1], None, caught_exception[2]
        else:
            # create a symbolic link to the annotation for every output file
            for tag in run._output_files.keys():
                for out_path in run._output_files[tag].keys():
                    if out_path != None:
                        destination_path = os.path.join(
                            self.get_output_directory(), 
                            '.' + os.path.basename(out_path) + 
                            '.annotation.yaml')
                        # overwrite the symbolic link if it already exists
                        if os.path.exists(destination_path):
                            os.unlink(destination_path)
                        oldwd = os.getcwd()
                        os.chdir(os.path.dirname(destination_path))
                        os.symlink(os.path.basename(annotation_path), 
                                   os.path.basename(destination_path))
                        os.chdir(oldwd)

            # finally, remove the temporary directory if it's empty
            try:
                os.rmdir(temp_directory)
            except OSError:
                pass
            
            # step has completed successfully, now determine how many jobs are
            # still left but first invalidate the FS cache because things have
            # changed by now...
            AbstractStep.fsc = fscache.FSCache()
            
            remaining_task_info = self.get_run_info_str()
            
            message = "[OK] %s/%s successfully finished on %s after %s\n" % \
                      (str(self), run_id, socket.gethostname(), 
                       misc.duration_to_str(self.end_time - self.start_time))
            message += str(self) + ': ' + remaining_task_info + "\n"
            attachment = None
            if os.path.exists(annotation_path + '.png'):
                attachment = dict()
                attachment['name'] = 'details.png'
                attachment['data'] = open(annotation_path + '.png').read()
            self.get_pipeline().notify(message, attachment)
            
            # and now... check whether we have any volatile parents. If we find
            # one, determine for each of its output files A whether all output
            # files B which depend on A are already in place and whether the
            # task which produced the output file B is finished. In that case,
            # we can truncate output file A and rename it to act as a 'volatile
            # placeholder'.
            task_id = '%s/%s' % (self, run_id)
            input_files = set()
            if task_id in self.get_pipeline().input_files_for_task_id:
                input_files = self.get_pipeline().input_files_for_task_id[task_id]
            candidate_tasks = set()
            # Only source steps do have empty strings in the input files list
            # so we can savely exclude them here
            for inpath in [x for x in input_files if x != '']:
                task_id = self.get_pipeline().task_id_for_output_file[inpath]
                if task_id in self.get_pipeline().task_for_task_id:
                    task = self.get_pipeline().task_for_task_id[task_id]
                    if task.step._options['_volatile'] == True:
                        candidate_tasks.add(task)
                    
            for task in candidate_tasks:
                task.volatilize_if_possible(srsly = True)
                                
            self._reset()

    def reports(self, run_id, out_connection_output_files):
        '''
        Abstract method this must be implemented by actual step.

        Raise NotImplementedError if subclass does not override this
        method.
        '''
        raise NotImplementedError()


    def generate_report(self, run_id):
        '''
        Gathers the output files for each outgoing connection and calls
        self.reports() to do the job of creating a report.
        '''

        run = self.get_run(run_id)
        out_connection_output_files = dict()
        for out_connection in run.get_out_connections():
            out_connection_output_files[out_connection] = run.\
                get_output_files_abspath_for_out_connection(out_connection)
            
        try:
            self.reports(run_id, out_connection_output_files)
        except NotImplementedError as e:
            logger.info('Step %s is not capable to generate reports' %
                        (self._step_name))
        except Exception as e:
            logger.error('Unexpected error while trying to generate report for '
                         'task %s/%s: %s' % (self._step_name, run_id,
                                             e))

    def get_pre_commands(self):
        """
        Return dictionary with commands to execute before starting any other
        command of this step
        """
        return self._pre_command

    def get_module_loads(self):
        """
        Return dictionary with module load commands to execute before starting
        any other command of this step
        """
        return self._module_load

    def get_tool(self, key):
        """
        Return full path to a configured tool.
        """
        if key not in self._tools:
            raise StandardError("Tool %s unknown. Maybe you forgot to use "
                                "self.require_tool('%s')" % (key, key))
        return self._tools[key]
    
    def get_module_unloads(self):
        """
        Return dictionary with module unload commands to execute before
        starting any other command of this step
        """
        return self._module_unload


    def get_post_commands(self):
        """
        Return dictionary with commands to execute after finishing any other
        command of this step
        """
        return self._post_command


    def get_run_info_str(self):
        count = {}
        for _ in self.get_run_ids():
            state = self.get_run_state(_)
            if not state in count:
                count[state] = 0
            count[state] += 1
        return ', '.join(["%d %s" % (count[_], _.lower()) \
                          for _ in self.get_pipeline().states.order if _ in count])
    
    def append_pipeline_log(self, log):
        if len(self._pipeline_log) == 0:
            self._pipeline_log = log
        else:
            for k in log.keys():
                if k == 'process_watcher':
                    for k2 in log[k].keys():
                        if k2 == 'max':
                            for _ in log[k][k2].keys():
                                if _ == 'sum':
                                    for k3 in self._pipeline_log[k][k2][_].keys():
                                        self._pipeline_log[k][k2][_][k3] = \
                                            max(self._pipeline_log[k][k2][_][k3],
                                                log[k][k2][_][k3])
                                else:
                                    self._pipeline_log[k][k2][_] = log[k][k2][_]
                        else:
                            self._pipeline_log[k][k2].update(log[k][k2])
                            
                else:
                    if log[k].__class__ == list:
                        self._pipeline_log[k].extend(log[k])
                    else:
                        self._pipeline_log[k].update(log[k])
    

    def __str__(self):
        return self._step_name
    
    @classmethod
    def render_pipeline(cls, logs):
        hash = {'nodes': {}, 'edges': {}, 'clusters': {}, 'graph_labels': {}}
        for log in logs:
            temp = cls.render_pipeline_hash(log)
            for _ in ['nodes', 'edges', 'clusters', 'graph_labels']:
                hash[_].update(temp[_])

        f = StringIO.StringIO()
        f.write("digraph {\n")
        f.write("    rankdir = TB;\n")
        f.write("    splines = true;\n")
        f.write("    graph [fontname = Helvetica, fontsize = 12, size = "
                "\"14, 11\", nodesep = 0.2, ranksep = 0.3, labelloc = t, "
                "labeljust = l];\n")
        f.write("    node [fontname = Helvetica, fontsize = 12, shape = rect, "
                "style = filled];\n")
        f.write("    edge [fontname = Helvetica, fontsize = 12];\n")
        f.write("\n")
        
        f.write("    // nodes\n")
        f.write("\n")
        for node_key, node_info in hash['nodes'].items():
            f.write("    _%s" % node_key)
            if len(node_info) > 0:
                f.write(" [%s]" % ', '.join(['%s = "%s"' % (k, node_info[k]) \
                                             for k in node_info.keys()]))
            f.write(";\n")
            
        f.write("\n")
        
        f.write("    // edges\n")
        f.write("\n")
        for edge_pair in hash['edges'].keys():
            if edge_pair[0] in hash['nodes'] and edge_pair[1] in hash['nodes']:
                f.write("    _%s -> _%s;\n" % (edge_pair[0], edge_pair[1]))
        
        f.write("\n")
        
        """
        f.write("    // clusters\n")
        f.write("\n")
        for cluster_hash, cluster_info in hash['clusters'].items():
            f.write("    subgraph cluster_%s {\n" % cluster_hash)
            for node in cluster_info['group']:
                f.write("        _%s;\n" % node)
                
            f.write("        label = \"%s\";\n" % cluster_info['task_name'])
            f.write("        graph [style = dashed];\n")
            f.write("    }\n")
        """
        
        if len(hash['graph_labels']) == 1:
            f.write("    graph [label=\"%s\"];\n" % 
                    hash['graph_labels'].values()[0])
        f.write("}\n")
        
        result = f.getvalue()
        f.close()
        return result
        
    @classmethod
    def render_pipeline_hash(cls, log):
        
        def pid_hash(pid, suffix = ''):
            hashtag = "%s/%s/%d/%s" % (log['step']['name'], 
                                       log['run']['run_id'], 
                                       pid, suffix)
            return misc.str_to_sha1(hashtag)
        
        def file_hash(path):
            if path in log['step']['known_paths']:
                if 'real_path' in log['step']['known_paths'][path]:
                    path = log['step']['known_paths'][path]['real_path']
            return misc.str_to_sha1(path)
        
        #print(yaml.dump(self.known_paths, default_flow_style = False))
        
        hash = dict()
        hash['nodes'] = dict()
        hash['edges'] = dict()
        hash['clusters'] = dict()
        hash['graph_labels'] = dict()
        
        def add_file_node(path):
            if not path in log['step']['known_paths']:
                return
                
            if 'real_path' in log['step']['known_paths'][path]:
                path = log['step']['known_paths'][path]['real_path']
            label = log['step']['known_paths'][path]['label']
            color = '#ffffff'
            if log['step']['known_paths'][path]['type'] == 'fifo':
                color = '#c4f099'
            elif log['step']['known_paths'][path]['type'] == 'file':
                color = '#8ae234'
            elif log['step']['known_paths'][path]['type'] == 'step_file':
                color = '#97b7c8'
                if path in log['step']['known_paths']:
                    if 'size' in log['step']['known_paths'][path]:
                        label += "\\n%s" % misc.bytes_to_str(
                            log['step']['known_paths'][path]['size'])
            hash['nodes'][misc.str_to_sha1(path)] = {
                'label': label,
                'fillcolor': color
            }
            
        for proc_info in copy.deepcopy(log['pipeline_log']['processes']):
            pid = proc_info['pid']
            label = "PID %d" % pid
            name = '(unknown)'
            if 'name' in proc_info:
                name = proc_info['name']
            label = "%s" % (proc_info['name'])
            if 'writes' in proc_info['hints']:
                for path in proc_info['hints']['writes']:
                    add_file_node(path)
            if 'args' in proc_info:
                stripped_args = []
                for arg in copy.deepcopy(proc_info['args']):
                    if arg in log['step']['known_paths']:
                        add_file_node(arg)
                    if arg in log['step']['known_paths']:
                        if log['step']['known_paths'][arg]['type'] != 'step_file':
                            arg = log['step']['known_paths'][arg]['label']
                        else:
                            arg = os.path.basename(arg)
                    else:
                        if arg[0:4] != '/dev':
                            arg = os.path.basename(arg)
                            if (len(arg) > 16) and re.match('^[A-Z]+$', arg):
                                arg = "%s[...]" % arg[:16]
                    stripped_args.append(arg.replace('\t', '\\t').replace(
                        '\\', '\\\\'))
                tw = textwrap.TextWrapper(
                    width = 50, 
                    break_long_words = False, 
                    break_on_hyphens = False)
                label = "%s" % ("\\n".join(tw.wrap(' '.join(stripped_args))))
            if 'args' in proc_info:
                cat4m_seen_minus_o = False
                for arg in proc_info['args']:
                    fifo_type = None
                    if name == 'cat4m' and arg == '-o':
                        cat4m_seen_minus_o = True
                    if arg in log['step']['known_paths']:
                        add_file_node(arg)
                        if name == 'cat4m':
                            if cat4m_seen_minus_o:
                                fifo_type = 'output'
                            else:
                                fifo_type = 'input'
                        else:
                            # we can't know whether the fifo is for input or
                            # output, first look at the hints, then use the
                            # designation (if any was given)
                            if 'reads' in proc_info['hints'] and \
                               arg in proc_info['hints']['reads']:
                                fifo_type = 'input'
                            if 'writes' in proc_info['hints'] and \
                               arg in proc_info['hints']['writes']:
                                fifo_type = 'output'
                            if fifo_type is None:
                                fifo_type = log['step']['known_paths'][arg]\
                                            ['designation']
                        if fifo_type == 'input':
                            # add edge from file to proc
                            hash['edges'][(file_hash(arg), pid_hash(pid))] \
                                = dict()
                        elif fifo_type == 'output':
                            # add edge from proc to file
                            hash['edges'][(pid_hash(pid), file_hash(arg))] \
                                = dict()
            if 'writes' in proc_info['hints']:
                for path in proc_info['hints']['writes']:
                    hash['edges'][(pid_hash(pid), file_hash(path))] = dict()
            # add proc
            something_went_wrong = False
            if 'signal' in proc_info:
                something_went_wrong = True
            elif 'exit_code' in proc_info:
                if proc_info['exit_code'] != 0:
                    something_went_wrong = True
            else:
                something_went_wrong = True
            color = "#fce94f"
            if something_went_wrong:
                if not pid in log['pipeline_log']['ok_to_fail']:
                    color = "#d5291a"
                if 'signal' in proc_info:
                    label = "%s\\n(received %s%s)" % (
                        label, 
                        'friendly ' \
                        if pid in log['pipeline_log']['ok_to_fail'] else '',
                        proc_info['signal_name'] if 'signal_name' in \
                        proc_info else 'signal %d' % proc_info['signal'])
                elif 'exit_code' in proc_info:
                    if proc_info['exit_code'] != 0:
                        label = "%s\\n(failed with exit code %d)" % (
                            label, proc_info['exit_code'])
                else:
                    label = "%s\\n(no exit code)" % label
                    
            if 'max' in log['pipeline_log']['process_watcher']:
                if pid in log['pipeline_log']['process_watcher']['max']:
                    label += "\\n%1.1f%% CPU, %s RAM (%1.1f%%)" % (
                        log['pipeline_log']['process_watcher']['max'][pid]\
                        ['cpu_percent'],
                        misc.bytes_to_str(
                            log['pipeline_log']['process_watcher']['max'][pid]\
                            ['rss']),
                        log['pipeline_log']['process_watcher']['max'][pid]\
                        ['memory_percent'])
                
            hash['nodes'][pid_hash(pid)] = {
                'label': label,
                'fillcolor': color
            }
            
            for which in ['stdout', 'stderr']:
                key = "%s_copy" % which
                if key in proc_info:
                    if ('exit_code' in proc_info[key]) and \
                       (proc_info[key]['exit_code'] == 0) and \
                       ('length' in proc_info[key]) and \
                       (proc_info[key]['length'] == 0) and \
                       (not 'sink_full_path' in proc_info[key]):
                        # skip this stdout/stderr box if it leads to nothing
                        continue
                    size_label = '(empty)'
                    if ('length' in proc_info[key]) and \
                       (proc_info[key]['length'] > 0):
                        speed = float(proc_info[key]['length']) / (
                            proc_info[key]['end_time'] - 
                            proc_info[key]['start_time']).total_seconds()
                        speed_label = "%s/s" % misc.bytes_to_str(speed)
                        size_label = "%s / %s lines (%s)" % (
                            misc.bytes_to_str(proc_info[key]['length']),
                            "{:,}".format(proc_info[key]['lines']),
                            speed_label)
                    label = "%s\\n%s" % (which, size_label)
                    
                    something_went_wrong = False
                    if 'signal' in proc_info[key]:
                        something_went_wrong = True
                    elif 'exit_code' in proc_info[key]:
                        if proc_info[key]['exit_code'] != 0:
                            something_went_wrong = True
                    else:
                        something_went_wrong = True
                    color = "#fdf3a7"
                    if something_went_wrong:
                        if not pid in log['pipeline_log']['ok_to_fail']:
                            color = "#d5291a"
                        if 'signal' in proc_info[key]:
                            label = "%s\\n(received %s%s)" % (
                                label, 
                                "friendly " if pid in \
                                log['pipeline_log']['ok_to_fail'] else '',
                                proc_info[key]['signal_name'] if 'signal_name'\
                                in proc_info[key] else 'signal %d' % 
                                proc_info[key]['signal'])
                        elif 'exit_code' in proc_info[key]:
                            if proc_info[key]['exit_code'] != 0:
                                label = "%s\\n(failed with exit code %d)" % (
                                    label, proc_info[key]['exit_code'])
                        else:
                            label = "%s\\n(no exit code)" % label
                            
                                
                    # add proc_which
                    hash['nodes'][pid_hash(pid, which)] = {
                        'label': label,
                        'fillcolor': color
                    }
                    if 'sink_full_path' in proc_info[key]:
                        path = proc_info[key]['sink_full_path']
                        add_file_node(path)

        for proc_info in copy.deepcopy(log['pipeline_log']['processes']):
            pid = proc_info['pid']
            if 'use_stdin_of' in proc_info:
                other_pid = proc_info['use_stdin_of']
                hash['edges'][(pid_hash(other_pid, 'stdout'), pid_hash(pid))] \
                    = dict()
            for which in ['stdout', 'stderr']:
                key = "%s_copy" % which
                if key in proc_info:
                    other_pid = proc_info[key]['pid']
                    hash['edges'][(pid_hash(pid), pid_hash(pid, which))] = dict()
                    if 'sink_full_path' in proc_info[key]:
                        hash['edges'][(
                            pid_hash(pid, which),
                            file_hash(proc_info[key]['sink_full_path']))] = dict()

        # define nodes which go into subgraph
        step_file_nodes = dict()
        for path, path_info in log['step']['known_paths'].items():
            if path_info['type'] == 'step_file':
                step_file_nodes[file_hash(path)] = path_info['designation']

        task_name = "%s/%s" % (log['step']['name'], log['run']['run_id'])
        cluster_hash = misc.str_to_sha1(task_name)
        hash['clusters'][cluster_hash] = dict()
        hash['clusters'][cluster_hash]['task_name'] = task_name
        hash['clusters'][cluster_hash]['group'] = list()
        for node in hash['nodes'].keys():
            if not node in step_file_nodes:
                hash['clusters'][cluster_hash]['group'].append(node)
                
        start_time = log['start_time']
        end_time = log['end_time']
        duration = end_time - start_time
        
        hash['graph_labels'][task_name] = "Task: %s\\lHost: %s\\lDuration: "
        "%s\\l" % (task_name, 
            socket.gethostname(),
            misc.duration_to_str(duration, long = True))
        if 'max' in log['pipeline_log']['process_watcher']:
            hash['graph_labels'][task_name] += "CPU: %1.1f%%, %d "
            "CORES_Requested , RAM: %s (%1.1f%%)\\l" % (
                log['pipeline_log']['process_watcher']['max']['sum']\
                ['cpu_percent'],
                log['step']['cores'],
                misc.bytes_to_str(log['pipeline_log']['process_watcher']['max']\
                                  ['sum']['rss']), 
                log['pipeline_log']['process_watcher']['max']['sum']\
                ['memory_percent'])
        if 'signal' in log:
            hash['graph_labels'][task_name] += "Caught signal: %s\\l" % \
            process_pool.ProcessPool.SIGNAL_NAMES[log['signal']]
        hash['graph_labels'][task_name] += "\\l"
        return hash

    @classmethod
    def get_step_class_for_key(cls, key):
        """
        Returns a step (or source step) class for a given key which corresponds
        to the name of the module the class is defined in. Pass 'cutadapt' and
        you will get the cutadapt.Cutadapt class which you may then instantiate.
        """

        # Attention, import statement in class method coming right up!
        # Ok, this is strange, I know. But we need the io_step.IOStep class now
        # because we want to test whether module members are a subclass of this
        # and if we import it right at the beginning of this file, we would create
        # a circular reference, because AbstractStep is imported at the beginning
        # of io_step. There's probably a better solution, but I think it doesn't
        # hurt, either. Here goes the awkward line:
        import io_step
        
        check_classes = [AbstractSourceStep, AbstractStep, io_step.IOStep]
        for index, c in enumerate(check_classes):
            classes = [_ for _ in inspect.getmembers(__import__(key), 
                                                     inspect.isclass) \
                       if c in _[1].__bases__]
            for k in range(index):
                classes = [_ for _ in classes if _[1] != check_classes[k]]
            if len(classes) > 0:
                if len(classes) != 1:
                    raise StandardError(
                        "need exactly one subclass of %s in %s" % (c, key))
                return classes[0][1]

        raise StandardError("No suitable class found for module %s." % key)
    
    def set_cores(self, cores):
        """
        Specify the number of CPU cores this step will use.
        """
        self._cores = cores

    def get_cores(self):
        """
        Returns the number of cores used in this step.
        """
        return self._cores

    def add_connection(self, connection, constraints = None):
        """
        Add a connection, which must start with 'in/' or 'out/'.
        """
        if not (connection[0:3] == 'in/' or connection[0:4] == 'out/'):
            raise StandardError("A connection must start with 'in/' or 'out/'.")
        if connection[0:3] == 'in/':
            self.needs_parents = True
        self._connections.add(connection)
        if constraints is not None:
            self._connection_restrictions[connection] = constraints
        
    def get_in_connections(self):
        """
        Return all in-connections for this step
        """
        connections = self._connections
        in_connections = set()
        for connection in connections:
            if connection[0:3] == "in/":
                in_connections.add(connection)
        return in_connections

    def get_out_connections(self):
        """
        Return all out-connections for this step
        """
        connections = self._connections
        out_connections = set()
        for connection in connections:
            if connection[0:3] == "out/":
                out_connections.add(connection)
        return out_connections

    def require_tool(self, tool):
        """
        Declare that this step requires an external tool. Query it later with 
        *get_tool()*.
        """
        if self.get_pipeline() is not None:
            if not tool in self.get_pipeline().config['tools']:
                raise StandardError("%s requires the tool %s but it's not "
                                    "declared in the configuration." % (
                                        self, tool))
            self._tools[tool] = self.get_pipeline().config['tools'][tool]['path']
            if 'pre_command' in self.get_pipeline().config['tools'][tool]:
                self._pre_command[tool] = self.get_pipeline().config['tools'][tool]\
                                          ['pre_command']
            if 'module_load' in self.get_pipeline().config['tools'][tool]:
                self._module_load[tool] = self.get_pipeline().config['tools'][tool]\
                                          ['module_load']
            if 'module_load' in self.get_pipeline().config['tools'][tool]:
                self._module_unload[tool] = self.get_pipeline().config['tools'][tool]\
                                            ['module_unload']
            if 'post_command' in self.get_pipeline().config['tools'][tool]:
                self._post_command[tool] = self.get_pipeline().config['tools'][tool]\
                                           ['post_command']
        else:
            self._tools[tool] = True

    def add_option(self, key, *option_types, **kwargs):
        """
        Add an option. Multiple types may be specified.
        """
        if not 'optional' in kwargs:
            kwargs['optional'] = False
        for _ in ['default', 'label', 'description', 'group', 'tools',
                  'choices']:
            if not _ in kwargs: 
                kwargs[_] = None

        if key[0] == '_':
            raise StandardError(
                "Option key must not start with an underscore: %s." % key)
        if key in self._defined_options:
            raise StandardError("Option %s is already defined." % key)
        if len(option_types) == 0:
            raise StandardError("No option type specified for option %s." % key)
        if len(option_types) > 1 and kwargs['choices'] != None:
            raise StandardError("You cannot define choices if multiple "
                                "options types are defined (%s)." % key)
        for option_type in option_types:
            if not  option_type in [int, float, str, bool, list, dict]:
                raise StandardError("Invalid type for option %s: %s." % 
                                    (key, option_type))
        if kwargs['optional'] and (kwargs['default'] != None):
            if type(kwargs['default']) not in option_types:
                raise StandardError(
                    "Type of default value (%s) does not match any of the "
                    "declared possible types (%s)." % 
                    (type(kwargs['default']), option_types))

        info = dict()
        info['types'] = option_types
        for _ in ['optional', 'default', 'label', 'description', 'group', 
                  'tools', 'choices']:
            info[_] = kwargs[_]

        self._defined_options[key] = info
        
    def find_upstream_info_for_input_paths_as_set(self, input_paths,
                                                  key, expected = 1):
        task_ids = set()
        for path in input_paths:
            task_ids.add(self.get_pipeline().task_id_for_output_file[path])
        results = set()
        for task_id in task_ids:
            task = self.get_pipeline().task_for_task_id[task_id]
            step = task.step
            run_id = task.run_id
            run = step._runs[run_id]
            if run.has_public_info(key):
            	results.add(run.get_public_info(key))
            results |= self.find_upstream_info_for_input_paths_as_set(
                task.input_files(), key, None)
        
        if expected is not None:
            if len(results) != expected:
                raise StandardError(
                    "Unable to determine upstream %s info from %s." % 
                    (key, self))
        return results
        
    def find_upstream_info_for_input_paths(self, input_paths, key):
        """
        Find a piece of public information in all upstream steps. If the 
        information is not found or defined in more than one upstream step,
        this will crash.
        """
        # And boy, will it crash. SUH-MAAAASH! http://youtu.be/PbYD7sj6vxc?t=1m38s

        result = self.find_upstream_info_for_input_paths_as_set(
            input_paths, key, expected = 1)
        return list(result)[0]
                                                                
    def get_option(self, key):
        """
        Query an option.
        """
        if key not in self._defined_options:
            raise StandardError(
                "Cannot query undefined option %s in step %s." % 
                (key, __module__))
        return self._options[key]

    def get_options(self):
        '''
        Returns a dictionary of all given options
        '''
        return self._options

    def is_option_set_in_config(self, key):
        """
        Determine whether an optional option (that is, a non-required option)
        has been set in the configuration.
        """
        if key not in self._defined_options:
            raise StandardError(
                "Cannot query undefined option %s in step %s." % 
                (key, __module__))
        return key in self._options

    def get_input_run_info_for_connection(self, in_key):
        if in_key[0:3] != 'in/':
            raise StandardError("in_key does not start with 'in/': %s" % in_key)
        if in_key not in self._connections:
            raise StandardError("Undeclared connection %s." % in_key)
        out_key = in_key.replace('in/', 'out/')
        allowed_steps = None
        if '_connect' in self._options:
            if in_key in self._options['_connect']:
                declaration = self._options['_connect'][in_key]
                if declaration.__class__ == str:
                    if '/' in declaration:
                        parts = declaration.split('/')
                        allowed_steps = set()
                        allowed_steps.add(parts[0])
                        out_key = 'out/' + parts[1]
                    else:
                        out_key = 'out/' + declaration
                else:
                    raise StandardError(
                        "Invalid _connect value: %s" % yaml.dump(declaration))
        
        result = dict()
        result['counts'] = {
            'total_steps': 0,
            'total_runs': 0,
            'total_files': 0,
            'min_steps_per_run': None,
            'max_steps_per_run': None,
            'min_files_per_step_and_run': None,
            'max_files_per_step_and_run': None,
            'min_files_per_run': None,
            'max_files_per_run': None,
        }
        
        def update_min_max(key, value):
            for mkey in ['min', 'max']:
                key2 = '%s_%s' % (mkey, key)
                if result['counts'][key2] is None:
                    result['counts'][key2] = value
                result['counts'][key2] = (min if mkey == 'min' else max)\
                                         (result['counts'][key2], value)
            
        result['runs'] = dict()
        for step_name, step_info in self.get_input_run_info().items():
            if allowed_steps is not None:
                if not step_name in allowed_steps:
                    continue
            for key in self.get_pipeline().steps[step_name]._connections:
                if out_key == 'out/*' or out_key == key:
                    result['counts']['total_steps'] += 1
                    for run_id, run_info in step_info.items():
                        result['counts']['total_runs'] += 1
                        paths = run_info.get_output_files_abspath()[key.replace(
                            'out/', '')].keys()
                        result['counts']['total_files'] += len(paths)
                        if not run_id in result['runs']:
                            result['runs'][run_id] = dict()
                        result['runs'][run_id][step_name] = paths

                        steps_per_run = len(result['runs'][run_id])
                        update_min_max('steps_per_run', steps_per_run)

                        files_per_step_and_run = len(result['runs'][run_id]\
                                                     [step_name])
                        update_min_max('files_per_step_and_run',
                                       files_per_step_and_run)
                        
                        files_per_run = 0
                        for _ in result['runs'][run_id].values():
                            files_per_run += len(_)
                        update_min_max('files_per_run', files_per_run)
                    
        # check constraints, if any
        if in_key in self._connection_restrictions:
            for k, v in self._connection_restrictions[in_key].items():
                if result['counts'][k] != v:
                    raise StandardError("Connection constraint failed: %s/%s"
                                        "/%s should be %d but is %s." % 
                                        (self, in_key, k, v, 
                                         str(result['counts'][k])))

        return result

    def get_run_ids_and_input_files_for_connection(self, in_key):
        """
        Returns an iterator/generator with run_id and input_files where:
            - run_id is a string
            - input_files is a list of input paths
        """
        result = self.get_input_run_info_for_connection(in_key)
        for run_id, info in result['runs'].items():
            input_files = list()
            for step_name, input_paths in info.items():
                input_files.extend(input_paths)
            input_files = sorted(input_files)
            yield run_id, input_files

    def get_run_ids_and_input_run_infos(self, in_key):
        pass

    def get_input_files_for_run_id_and_connection(self, run_id, in_key):
        """
        Returns a list of all input files given a run_id and a connection
        """
        result = self.get_input_run_info_for_connection(in_key)
        info = result['runs'][run_id]
        input_files = list()
        for step_name, input_paths in info.items():
            input_files.extend(input_paths)
        input_files = sorted(input_files)
        return input_files
        
        

    def get_n_input_file_for_connection(self, in_key, expected):
        result = self.get_input_run_info_for_connection(in_key)
        values = set()
        for run_id, info in result['runs'].items():
            for step_name, input_paths in info.items():
                for path in input_paths:
                    values.add(path)
        if len(values) != expected:
            raise StandardError("Expected exactly %d files for %s in %s, "
                                "got %d instead." % 
                                (expected, in_key, self, len(values)))
        return list(values)
        
    def get_single_input_file_for_connection(self, in_key):
        """
        Return a single input file for a given connection, also make sure that
        there's exactly one such input file.
        """
        return self.get_n_input_file_for_connection(in_key, 1)[0]

    def get_annotation_for_input_file(self, path):
        """
        Determine the annotation for a given input file (that is, the connection
        name).
        """
        # that's four nested loops
        for dep in self.dependencies:
            run_info = dep.get_runs()
            for run_id, run in run_info.items():
                for annotation, in_paths in run.get_output_files_abspath().items():
                    for in_path in in_paths:
                        if path == in_path:
                            return annotation
        raise StandardError(
            "Unable to determine annotation type for input file %s." % path)

    
class AbstractSourceStep(AbstractStep):
    """
    A subclass all source steps inherit from and which distinguishes source
    steps from all real processing steps because they do not yield any tasks, 
    because their "output files" are in fact files which are already there.
    
    Note that the name might be a bit misleading because this class only
    applies to source steps which 'serve' existing files. A step which has 
    no input but produces input data for other steps and actually has to do 
    something for it, on the other hand, would be a normal AbstractStep
    subclass because it produces tasks.
    """

    def __init__(self, pipeline):
        super(AbstractSourceStep, self).__init__(pipeline)
