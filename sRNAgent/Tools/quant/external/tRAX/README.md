## tRAX

### [Official docs](http://trna.ucsc.edu/tRAX)

### About
tRNA Analysis of eXpression (tRAX) is a software package for quantifying tRNA-derived small RNAs (tDRs), mature tRNAs, and related RNA features from sequencing data. This fork keeps the custom tRNA reference database build step and the mapping/counting workflow, but removes adapter trimming, differential analysis, visualization, QA reporting, coverage plotting, and track hub generation. Input FASTQ files are expected to already be in the form you want to quantify.

`processsamples.py` now produces quantification tables only.

### System requirements
tRAX requires to be run on a Linux/Unix system with at least 8 cores and 16 GB memory. Due to the large size of sequencing data, we do not recommend using tRAX on a regular desktop or laptop. The following dependencies have been tested, use newer versions at your own risk.

#### Dependencies
* Python 3.10
* pysam 0.20.0
  * Older versions have a memory leak, make sure you have an updated version
* bowtie2 2.5.1
* samtools 1.16.1
* Infernal 1.1.4 or higher
* The TestRun.bash script requires:
  * SRA toolkit(fastq-dump) 3.0.3


### Using Docker Image
To eliminate the need of installing dependencies, you can download the Docker image from our [DockerHub repository](https://hub.docker.com/r/ucsclowelab/trax) using the command
```
docker pull ucsclowelab/trax
```

### Using Conda Enviroment
In addition to Docker you can alternatively use a Conda environment using the command
```
conda env create -f trax_env.yaml
```

### Quantification workflow
Build a custom reference database first:
```
maketrnadb.py --databasename=db --genomefile=genome.fa --trnascanfile=tRNAs.out --namemapfile=tRNAs_name_map.txt
```

Then quantify already-prepared FASTQ files:
```
processsamples.py --experimentname=Experiment --databasename=db --samplefile=samples.txt --ensemblgtf=genes.gtf
```

The main quantification tables are written under the experiment directory, including `*-readcounts.txt`, `*-trnacounts.txt`, `*-typecounts.txt`, `*-aminocounts.txt`, `*-anticodoncounts.txt`, and files in `unique/`.

### [Quickstart and tutorial](http://trna.ucsc.edu/tRAX/#tutorial)
