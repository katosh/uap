from uaperrors import UAPError
import os
from logging import getLogger
from abstract_step import AbstractStep

logger = getLogger('uap_logger')

class ChromHmmBinarizeBam(AbstractStep):
    '''
    This command converts coordinates of aligned reads into binarized data form
    from which a chromatin state model can be learned. The binarization is based
    on a poisson background model. If no control data is specified the parameter
    to the poisson distribution is the global average number of reads per bin.
    If control data is specified the global average number of reads is
    multiplied by the local enrichment for control reads as determined by the
    specified parameters. Optionally intermediate signal files can also be
    outputted and these signal files can later be directly converted into binary
    form using the BinarizeSignal command.
    '''

    def __init__(self, pipeline):
        super(ChromHmmBinarizeBam, self).__init__(pipeline)

        self.set_cores(8)

        self.add_connection('in/alignments')
        self.add_connection('out/alignments')
        self.add_connection('out/metrics')

        self.require_tool('ChromHMM')
        self.require_tool('echo')
        self.require_tool('ln')

        self.add_option('chrom_sizes_file', str, optional = False,
                        description = "File containing chromosome size "
                        "information generated by 'fetchChromSizes'")
        self.add_option('control', dict, optional = False)

        # ChromHMM BinarizeBam Options
        self.add_option('b', int, optional = True,
                        description = "The number of base pairs in a bin "
                        "determining the resolution of the model learning and "
                        "segmentation. By default this parameter value is set "
                        "to 200 base pairs.")
        self.add_option('c', str, optional = True,
                        description = "A directory containing the control "
                        "input files. If this is not specified then the "
                        "inputbamdir is used. If no control files are specified "
                        "then by default a uniform background will be used in "
                        "determining the binarization thresholds.")
        self.add_option('center', bool, optional = True,
                        description = "If this flag is present then the center "
                        "of the interval is used to determine the bin to "
                        "assign a read. This can make sense to use if the "
                        "coordinates are based on already extended reads. If "
                        "this option is selected, then the strand information "
                        "of a read and the shift parameter are ignored. By "
                        "default reads are assigned to a bin based on the "
                        "position of its 5' end as determined from the strand "
                        "of the read after shifting an amount determined by "
                        "the -n shift option.")
        self.add_option('e', int, optional = True,
                        description = "Specifies the amount that should be "
                        "subtracted from the end coordinate of a read so that "
                        "both coordinates are inclusive and 0 based. The "
                        "default value is 1 corresponding to standard bed "
                        "convention of the end interval being 0-based but not "
                        "inclusive.")
        self.add_option('f', int, optional = True,
                        description = "This indicates a threshold for the fold "
                        "enrichment over expected that must be met or exceeded "
                        "by the observed count in a bin for a present call. "
                        "The expectation is determined in the same way as the "
                        "mean parameter for the poission distribution in terms "
                        "of being based on a uniform background unless control "
                        "data is specified. This parameter can be useful when "
                        "dealing with very deeply and/or unevenly sequenced "
                        "data. By default this parameter value is 0 meaning "
                        "effectively it is not used.")
        self.add_option('g', int, optional = True,
                        description = "This indicates a threshold for the "
                        "signal that must be met or exceeded by the observed "
                        "count in a bin for a present call. This parameter can "
                        "be useful when desiring to directly place a threshold "
                        "on the signal. By default this parameter value is 0 "
                        "meaning effectively it is not used.")
        self.add_option('n', int, optional = True,
                        description = "The number of bases a read should be "
                        "shifted to determine a bin assignment. Bin assignment "
                        "is based on the 5' end of a read shifted this amount "
                        "with respect to the strand orientation. By default "
                        "this value is 100.")
        self.add_option('o', str, optional = True,
                        description = "This specifies the directory to which "
                        "control data should be printed. The files will be "
                        "named CELL_CHROM_controlsignal.txt. Control data "
                        "will only be outputted if there are control bed files "
                        "present and an output control directory is specified.")
        self.add_option('p', float, optional = True,
                        description = "This option specifies the tail "
                        "probability of the poisson distribution that the "
                        "binarization threshold should correspond to. The "
                        "default value of this parameter is 0.0001.")
        self.add_option('peaks', bool, optional = True,
                        description = "This option specifies to treat the bed "
                        "files as peak calls directly and give a '1' call to "
                        "any bin overlapping a peak call.")
        self.add_option('s', int, optional = True,
                        description = "The amount that should be subtracted "
                        "from the interval start coordinate so the interval is "
                        "inclusive and 0 based. Default is 0 corresponding to "
                        "the standard bed convention.")
        self.add_option('strictthresh', bool, optional = True,
                        description = "If this flag is present then the "
                        "poisson threshold must be strictly greater than the "
                        "tail probability, otherwise by default the largest "
                        "integer count for which the tail includes the poisson "
                        "threshold probability is used.")
        self.add_option('t', str, optional = True)
        self.add_option('u', int, optional = True,
                        description = "An integer pseudocount that is "
                        "uniformly added to every bin in the control data in "
                        "order to smooth the control data from 0. The default "
                        "value is 1.")
        self.add_option('w', int, optional = True,
                        description = "This determines the extent of the "
                        "spatial smoothing in computing the local enrichment "
                        "for control reads. The local enrichment for control "
                        "signal in the x-th bin on the chromosome after "
                        "adding pseudocountcontrol is computed based on the "
                        "average control counts for all bins within x-w and "
                        "x+w. If no controldir is specified, then this option "
                        "is ignored. The default value is 5.")

    def runs(self, run_ids_connections_files):

        options = ['b', 'c', 'center', 'e', 'f', 'g', 'n', 'o', 'p', 'peaks',
                   's', 'strictthresh', 't', 'u', 'w']

        set_options = [option for option in options if \
                       self.is_option_set_in_config(option)]

        option_list = list()
        for option in set_options:
            if isinstance(self.get_option(option), bool):
                # todo as: condition senseless
                if self.get_option(option):
                    option_list.append('-%s' % option)
                else:
                    option_list.append('-%s' % option)
            else:
                option_list.append('-%s' % option)
                option_list.append(str(self.get_option(option)))


        # We need to create a cell-mark-file table file. Should look something
        # like this:
        #
        # cell1 mark1 cell1_mark1.bed cell1_control.bed
        # cell1 mark2 cell1_mark2.bed cell1_control.bed
        # cell2 mark1 cell2_mark1.bed cell2_control.bed
        # cell2 mark2 cell2_mark2.bed cell2_control.bed
        #
        # The control file is optional!!!

        # How can we get the cell and mark information?
        # Cell = key of self.get_option(control)
        # Mark = value of self.get_option(control)


        control_samples = self.get_option('control')
        for control_id, treatment_list in control_samples.iteritems():
            control = control_id
            # Check for existence of control files
            control_files = list()
            if control_id != 'None':
                try:
                    control_files = run_ids_connections_files[control_id]\
                                    ['in/alignments']
                    control_id = "-" + control_id
                except KeyError as e:
                    logger.info("Option 'control':\n"
                                 "No control '%s' found.\n" % control_id)

            # Check for existence of treatment files
            for tr in treatment_list:
                treatments = dict()
                try:
                    treatments[tr] = run_ids_connections_files[tr]\
                                     ['in/alignments']
                except KeyError as e:
                    logger.info("Option 'control':\n"
                                 "No treatment '%s' for control '%s' found."
                                 % (tr, control_id) )

                # Assemble rund ID
                run_id = "%s%s" % (tr, control_id)

                # Create list of input files
                input_paths = [f for l in [treatments[tr], control_files]\
                               for f in l]

                with self.declare_run(run_id) as run:

                    # temp directory = inputbamdir
                    temp_dir = run.get_output_directory_du_jour_placeholder()

                    # necessary for cell-mark-file table
                    linked_controls = list()
                    linked_treatments = list()

                    # Create links to all input paths in temp_dir
                    with run.new_exec_group() as exec_group:
                        for files, links in [[control_files, linked_controls], \
                                             [treatments[tr], linked_treatments]]:
                            for f in files:
                                f_basename = os.path.basename(f)
                                temp_f = run.add_temporary_file(
                                    suffix = f_basename)
                                ln = [self.get_tool('ln'), '-s', f, temp_f]
                                exec_group.add_command(ln)

                                # Save basename of created link
                                links.append(os.path.basename(temp_f))

                        logger.info("Controls: %s" %
                                     ", ".join(linked_controls))
                        logger.info("Treatments: %s" %
                                     ", ".join(linked_treatments))

                        # Create the table file
                        cell_mark_file_content = str()
                        for lt in linked_treatments:
                            line = "%s\t%s\t%s" % (control, tr, lt)
                            if linked_controls:
                                for lc in linked_controls:
                                    line += "\t%s" % lc
                            cell_mark_file_content += "%s\n" % line
                        logger.info(cell_mark_file_content)
                        echo = [self.get_tool('echo'), cell_mark_file_content]

                        cell_mark_file = run.add_temporary_file(suffix = run_id)
                        exec_group.add_command(echo, stdout_path = cell_mark_file)


                    with run.new_exec_group() as exec_group:
                        chromhmm = [ self.get_tool('ChromHMM'),
                                     'BinarizeBam',
                                     self.get_option('chrom_sizes_file'),
                                     temp_dir,
                                     cell_mark_file,
                                     temp_dir
                                 ]

