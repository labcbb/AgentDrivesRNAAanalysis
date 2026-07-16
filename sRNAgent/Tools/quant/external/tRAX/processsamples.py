#!/usr/bin/env python3

import argparse
import os
import re
import subprocess
import sys
import time
from multiprocessing import cpu_count

from packaging import version

import countreads
import countreadtypes
import mapreads
from trnasequtils import *


parser = argparse.ArgumentParser(description="Process tRNA experiment and produce quantification tables.")
parser.add_argument("--experimentname", required=True, help="experiment name to be used")
parser.add_argument("--databasename", required=True, help="name of the tRNA database")
parser.add_argument("--samplefile", required=True, help="sample file")
parser.add_argument("--ensemblgtf", help="The ensembl gene list for that species")
parser.add_argument("--bedfile", nargs="*", help="Additional bed files for feature list")
parser.add_argument("--lazyremap", action="store_true", default=False, help="Skip mapping reads if bam files exit")
parser.add_argument("--nofrag", action="store_true", default=False, help="Omit fragment determination (Used for TGIRT mapping)")
parser.add_argument("--maxmismatches", help="Maximum allowed mismatches")
parser.add_argument("--minnontrnasize", type=int, default=20, help="Minimum read length for non-tRNAs")
parser.add_argument("--maponly", action="store_true", default=False, help="Only do the mapping step")
parser.add_argument("--dumpother", action="store_true", default=False, help='Dump "other" features when counting gene types')
parser.add_argument("--local", action="store_true", default=False, help="use local bam mapping")
parser.add_argument("--cores", help="number of cores to use")
parser.add_argument("--skipfqcheck", action="store_true", default=False, help="Skips the check that the fq files match bam files")
parser.add_argument("--bamdir", help="directory for placing bam files (default current working directory)")


class trnadatabase:
    def __init__(self, dbname):
        self.dbname = dbname
        self.trnatable = dbname + "-trnatable.txt"
        self.bowtiedb = dbname + "-tRNAgenome"
        self.locifile = dbname + "-trnaloci.bed"
        self.maturetrnas = dbname + "-maturetRNAs.bed"
        self.trnaalign = dbname + "-trnaalign.stk"
        self.locialign = dbname + "-trnaloci.stk"
        self.trnanums = dbname + "-alignnum.txt"
        self.locinums = dbname + "-locusnum.txt"
        self.trnafasta = dbname + "-maturetRNAs.fa"
        self.modomics = dbname + "-modomics.txt"
        self.otherseqs = dbname + "-otherseqs.txt"
        self.dbinfo = dbname + "-dbinfo.txt"

    def getorgtype(self):
        orgtype = "euk"
        for currline in open(self.dbinfo):
            fields = currline.split()
            if fields[0] == "orgmode":
                orgtype = fields[1]
        return orgtype


class expdatabase:
    def __init__(self, expname):
        self.expname = expname
        self.uniquename = expname + "/unique/" + expname + "-unique"
        self.allfeats = expname + "/" + expname + "-allfeats.bed"
        self.mapinfo = expname + "/" + expname + "-mapinfo.txt"
        self.trnamapfile = expname + "/" + expname + "-trnamapinfo.txt"
        self.maplog = expname + "/" + expname + "-mapstats.txt"
        self.genetypes = expname + "/" + expname + "-genetypes.txt"
        self.genecounts = expname + "/" + expname + "-readcounts.txt"
        self.trnacounts = expname + "/" + expname + "-trnacounts.txt"
        self.genetypecounts = expname + "/" + expname + "-typecounts.txt"
        self.genetyperealcounts = expname + "/" + expname + "-typerealcounts.txt"
        self.trnaaminofile = expname + "/" + expname + "-aminocounts.txt"
        self.trnaanticodonfile = expname + "/" + expname + "-anticodoncounts.txt"
        self.trnalengthfile = expname + "/" + expname + "-readlengths.txt"
        self.mismatchcountfile = expname + "/" + expname + "-mismatches.txt"
        self.trnauniquefile = expname + "/unique/" + expname + "-trnauniquecounts.txt"
        self.trnaendfile = expname + "/" + expname + "-trnaendcounts.txt"


def makefeaturebed(trnainfo, expinfo, ensgtf, bedfiles):
    allfeatfile = open(expinfo.allfeats, "w")
    for currfeature in readbed(trnainfo.maturetrnas):
        print(currfeature.bedstring(), file=allfeatfile)
    for currfeature in readbed(trnainfo.locifile):
        print(currfeature.bedstring(), file=allfeatfile)
    if ensgtf is not None:
        for currfeature in readgtf(ensgtf):
            print(currfeature.bedstring(name=currfeature.data["genename"]), file=allfeatfile)
    for currbed in bedfiles:
        for currfeature in readbed(currbed):
            print(currfeature.bedstring(), file=allfeatfile)
    allfeatfile.close()


def mapsamples(samplefile, trnainfo, expinfo, lazyremap, bamdir="./", cores=8, minnontrnasize=20, local=False, skipfqcheck=False):
    mapreads.main(samplefile=samplefile, trnafile=trnainfo.trnatable, bowtiedb=trnainfo.bowtiedb, bamdir=bamdir, otherseqs=trnainfo.otherseqs, logfile=expinfo.maplog, mapfile=expinfo.mapinfo, trnamapfile=expinfo.trnamapfile, lazy=lazyremap, cores=cores, minnontrnasize=minnontrnasize, local=local, skipfqcheck=skipfqcheck)


def countfeatures(samplefile, trnainfo, expinfo, ensgtf, bedfiles, bamdir="./", cores=8, maxmismatches=None, nofrag=False):
    countreads.testmain(samplefile=samplefile, ensemblgtf=ensgtf, maturetrnas=[trnainfo.maturetrnas], bamdir=bamdir, otherseqs=trnainfo.otherseqs, trnaloci=[trnainfo.locifile], removepseudo=True, genetypefile=expinfo.genetypes, trnatable=trnainfo.trnatable, countfile=expinfo.genecounts, bedfile=bedfiles, trnacounts=expinfo.trnacounts, trnaends=expinfo.trnaendfile, trnauniquecounts=expinfo.trnauniquefile, nofrag=nofrag, cores=cores, maxmismatches=maxmismatches)


def counttypes(samplefile, trnainfo, expinfo, ensgtf, bedfiles, bamdir="./", countfrags=False, bamnofeature=False, fraguniq=True, cores=8):
    countreadtypes.main(combinereps=True, samplefile=samplefile, maturetrnas=[trnainfo.maturetrnas], otherseqs=trnainfo.otherseqs, bamdir=bamdir, trnatable=trnainfo.trnatable, trnaaminofile=expinfo.trnaaminofile, trnaanticodonfile=expinfo.trnaanticodonfile, ensemblgtf=ensgtf, trnaloci=[trnainfo.locifile], countfile=expinfo.genetypecounts, realcountfile=expinfo.genetyperealcounts, mismatchfile=expinfo.mismatchcountfile, bedfile=bedfiles, readlengthfile=expinfo.trnalengthfile, countfrags=countfrags, bamnofeature=bamnofeature, uniquename=expinfo.uniquename, fraguniq=fraguniq, cores=cores)


def testsamtools():
    samversionre = re.compile(r"Version\:\s*([\.\d]+)")
    samtoolsloc = get_location("samtools")
    if samtoolsloc is None:
        print("Cannot find samtools in path", file=sys.stderr)
        print("Make sure samtools is installed", file=sys.stderr)
        sys.exit(1)
    samtoolsjob = subprocess.Popen([samtoolsloc, "--help"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    samtoolsresults = samtoolsjob.communicate()[0]
    if samtoolsjob.returncode != 0:
        print("Samtools failed to run", file=sys.stderr)
        print("Make sure samtools is functioning", file=sys.stderr)
        sys.exit(1)
    samtoolsres = samversionre.search(samtoolsresults)
    if samtoolsres:
        if version.parse(samtoolsres.group(1)) < version.parse("1.0.0"):
            print("Old samtools version " + samtoolsres.group(1) + " found", file=sys.stderr)
            print("Upgrade to latest version", file=sys.stderr)
            sys.exit(1)
    else:
        print("Could not find samtools version number", file=sys.stderr)


args = parser.parse_args()
dbname = os.path.expanduser(args.databasename)
expname = args.experimentname
ensgtf = os.path.expanduser(args.ensemblgtf) if args.ensemblgtf is not None else None
samplefilename = os.path.expanduser(args.samplefile)
lazyremap = args.lazyremap
bedfiles = [] if args.bedfile is None else [os.path.expanduser(curr) for curr in args.bedfile]
nofrag = args.nofrag
bamdir = args.bamdir if args.bamdir is not None else "./"
maponly = args.maponly
local = args.local
maxmismatches = args.maxmismatches
skipfqcheck = args.skipfqcheck
splittypecounts = False
bamnofeature = args.dumpother
minnontrnasize = args.minnontrnasize
cores = min(8, cpu_count()) if args.cores is None else int(args.cores)
scriptdir = os.path.dirname(os.path.realpath(sys.argv[0])) + "/"

testsamtools()
get_location("bowtie2")
gitversion, gitversionhash = getgithash(scriptdir)

sampledata = samplefile(samplefilename)
samples = sampledata.getsamples()
if len(samples) > len(set(samples)):
    print("duplicate sample names in first column of sample file", file=sys.stderr)
    sys.exit(1)

for currsample in samples:
    if "-" in currsample:
        print("Sample names containing '-' character are not allowed", file=sys.stderr)
        sys.exit(1)
    if currsample[0].isdigit():
        print("Sample names starting with digits are not allowed", file=sys.stderr)
        sys.exit(1)

for currsample in sampledata.allreplicates():
    if "-" in currsample:
        print("Sample names containing '-' character are not allowed", file=sys.stderr)
        sys.exit(1)
    if currsample[0].isdigit():
        print("Sample names starting with digits are not allowed", file=sys.stderr)
        sys.exit(1)

if not os.path.exists(expname):
    os.makedirs(expname)
if not os.path.exists(expname + "/unique"):
    os.makedirs(expname + "/unique")

trnainfo = trnadatabase(dbname)
expinfo = expdatabase(expname)

runtime = time.time()
loctime = time.localtime(runtime)
print("Mapping Reads", file=sys.stderr)
mapsamples(samplefilename, trnainfo, expinfo, lazyremap, bamdir=bamdir, cores=cores, minnontrnasize=minnontrnasize, local=local, skipfqcheck=skipfqcheck)

runinfoname = expname + "/" + expname + "-runinfo.txt"
if not lazyremap:
    dbinfo = open(runinfoname, "w")
    print("Starting", file=dbinfo)
else:
    dbinfo = open(runinfoname, "a")
    print("---------------------------------------------------------", file=dbinfo)
    print("redoing", file=dbinfo)

print("expname\t" + expname, file=dbinfo)
print("time\t" + str(runtime) + " (" + str(loctime[1]) + "/" + str(loctime[2]) + "/" + str(loctime[0]) + ")", file=dbinfo)
print("samplefile\t" + os.path.realpath(samplefilename), file=dbinfo)
print("dbname\t" + os.path.realpath(dbname), file=dbinfo)
print("git version\t" + gitversion, file=dbinfo)
print("git version hash\t" + gitversionhash, file=dbinfo)
print("command\t" + " ".join(sys.argv), file=dbinfo)
dbinfo.close()

if maponly:
    sys.exit()

makefeaturebed(trnainfo, expinfo, ensgtf, bedfiles)
print("Counting Reads", file=sys.stderr)
countfeatures(samplefilename, trnainfo, expinfo, ensgtf, bedfiles, bamdir=bamdir, cores=cores, maxmismatches=maxmismatches, nofrag=nofrag)

print("Counting Read Types", file=sys.stderr)
counttypes(samplefilename, trnainfo, expinfo, ensgtf, bedfiles, bamdir=bamdir, countfrags=splittypecounts, bamnofeature=bamnofeature, fraguniq=not nofrag, cores=cores)
