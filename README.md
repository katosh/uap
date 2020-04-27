# uap -- Universal Analysis Pipeline

The **uap** package is a framework to configure, run, and control
large data multi-step analyses.
Its main focus is on the analysis of high-throughput sequencing data.

The aim of this data processing pipeline is to enable robust and straightforward
bioinformatics data evaluation.
It is implemented in Python, runs under GNU/Linux and can be controlled from the
command-line interface.
Although the primary focus is the evaluation of sequencing data, its design
allows for a variety of other applications.

## About this Repository

This repository contains the development status of **uap** at Fraunhofer IZI.
It is based on the **uap** repository as published in [Kämpf, C., Specht, M.,Scholz, A. et al. uap: reproducible and robust HTS data analysis. BMC Bioinformatics 20, 664 (2019)](https://doi.org/10.1186/s12859-019-3219-1), which is located [here](https://github.com/yigbt/uap).
 
The current version [v1.0](https://github.com/yigbt/uap/releases/tag/v1.0) has been extended:

* code conversion from Python2 to Python3
* improved user interaction
* enhanced error detection for configuration
* validation of existing results by using annotation as configuration and recalculation of SHA256
* `status --details` completely lists errors or changes caused by adaptation of the configuration 
* enhanced detection of changes (software version, output files, sha256 of results (optional))
* improved error-management
* removed checksum suffix in output directories
* extended backward-compatible connection-management
* Source_controller step to check input data
* no need to configure `uap` internal scripts, GNU coreutils and `lmod`
* improved job-execution (signal handling, array jobs, enhanced logging, changes to configuration do not impact running jobs)
* processes are executed in temporary directories
* error fixing and code improvement

Please note, the current version of **uap** requires Python >= 3.5 and is only tested on [SLURM](https://slurm.schedmd.com/documentation.html).