#!/usr/bin/python3
import argparse
import subprocess
import os.path
import sys
from collections import defaultdict

### parse the arguments
parser = argparse.ArgumentParser()
parser.add_argument("-b", "--bedgraphFiles", metavar="IN_FILE", action='append', required=True, help="path to sorted BEDGRAPH files; at least two files must be given; all files must contain the same chromosomes in the same order")
parser.add_argument("-o", "--outputFile", metavar="OUT_FILE", required=True, help="path to the output file")
parser.add_argument("-i", "--mergedIdxstatsFile", metavar="IN_FILE", required=True, help="path to a tab-separated file that contains the output generated by samtools idxstats for all samples (columns 1-4) and in the 5th column the sample name; used columns: 1 -> chr name; 3 -> number of mapped reads; 5 -> name of the sample")
parser.add_argument("-n", "--notSkipHead", help="disables the skipping of the first line of the merged idxstats file (--mergedIdxstatsFile); default: first line is skipped", default = False, action='store_true')
parser.add_argument("-c", "--normByReadCount", metavar="N", type=int, help="number of reads to which each replicate is normed (based on the idxstats output) before values are averaged; default: 1000000", default = 1000000)
parser.add_argument("-d", "--numberOfDigits", metavar="N", type=int, help="number of decimal places to round the calculated scores; default: 5", default = 5)
args = parser.parse_args()

### var definition
processedChrs = []

### global vars
digitsToRound = 5
bedHandles = dict()
scalingFactors = dict()
chrsToProcess = dict()
lastBufferedElementStore = dict() # contains the last range that was read into the buffer in order to ensure consecutive ranges
buffers = dict()
buffersCurrentChr = dict()
bufferNoMoreData = dict()
removeRangesKeys = []
keepSamples = dict()
numberOfFiles = 0
###

### range definition
class Range:
	set = 0
	end = 0
	value = 0

	def __init__(self, start, end, value):
		self.start = start
		self.end = end
		self.value = value

	def length(self):
		return self.end - self.start + 1

	def getOutputFormat(self, chr):
		return '\t'.join([chr, str(self.start), str(self.end), str(self.value)])
###

### function definitions
# print error to stderr
def eprint(*args, **kwargs):
	print(*args, file=sys.stderr, **kwargs)
###

# tests, if two ranges are consecutive
# if not a spacing element is returned
def getConsecutiveRanges(f, l):

	# add start
	if f is None:
		return Range(0, l.start, 0)

	# test if the first two are connected
	if f.end != l.start:
		return Range(f.end, l.start, 0)

	return None
###

# try to fill the buffer with index from file with maximal n entries from the chr that is currently processed
def fillBuffer(index, n = 100000):
	global bedHandles, scalingFactors, buffersCurrentChr, bufferNoMoreData, chrsToProcess, lastBufferedElementStore

	if bufferNoMoreData[index] == True:
		return False

	artificial = 0
	# get FH
	fh = bedHandles[index]
	buf = buffers[index]
	initialBufSize = len(buf)

	if initialBufSize > 1:
		eprint("buffer %i is not empty!" % (index))
		exit(1)

	lastReadRange = lastBufferedElementStore[index]
	lastChr = buffersCurrentChr[index]
	lastPos = fh.tell()
	scale = scalingFactors[index]
	line = fh.readline()
	while line:
		tmp = line.strip().split("\t")
		chr = tmp[0]
		
		# do not process chrs that are not part of chrsToProcess
		if chr not in chrsToProcess:
			# read next line
			line = fh.readline()
			continue

		# check, if the same chr is processed
		if lastChr is None or lastChr == chr:
			start = int(tmp[1])
			end = int(tmp[2])
			value = int(float(tmp[3]))
			lastChr = chr
			lastPos = fh.tell()

			# create new range			
			newRange = Range(start, end, value / scale)

			# test if a spacer must be added
			spacer = getConsecutiveRanges(lastReadRange, newRange)
			if not spacer is None:
				buf.append(spacer)
				artificial += 1
						
			# store the new read range and update last read range	
			buf.append(newRange)
			lastReadRange = newRange

			# test if buffer is full
			if len(buf) >= n:
				line = None
			else:
				# read next line
				line = fh.readline()
		else:
			bufferNoMoreData[index] = True
			fh.seek(lastPos)
			line = None

	# test if some data was read
	if len(buf) > initialBufSize:
		# store the last element that was read into that buffer
		lastBufferedElementStore[index] = buf[len(buf)-1]

		buffersCurrentChr[index] = lastChr
		print("loaded %i ranges for sample with index %i on %s" % ((len(buf)-initialBufSize-artificial), index, lastChr))
		return True
	else:
		return False
###

# fill all buffers with next chr data when all buffers are empty
def processNextChrInFile(ranges):
	global buffers, bufferNoMoreData, buffersCurrentChr, lastPos, currentChr
	# fill the buffers
	for index in buffers:
		# reset help variables
		bufferNoMoreData[index] = False
		buffersCurrentChr[index] = None
		lastBufferedElementStore[index] = None
		buffers[index] = []

		# try to fill the buffer
		if not fillBuffer(index):
			buffers[index] = []

	# ensure that all buffers contain the same chr
	chr = buffersCurrentChr[1]
	for index in buffers:
		otherChr = buffersCurrentChr[index]
		if chr != otherChr:
			eprint("[ERROR] Not all files contain the same chromosomes in the same order! (%s vs %s)" % (chr, otherChr))
			exit(1)

	# check, if anything was read
	if chr is None:
		return False

	# update position to process and init the range dictionary
	lastPos = 0
	currentChr = chr
	initRanges(ranges)
	return True
###

# get the next range from all files for the current chr
def getNextRangesToProcess(ranges, index):
	global buffers
	if len(buffers[index]) > 0:
		ranges[index] = buffers[index].pop(0)
	else:
		# try to refill the buffer with data from the current chr
		if fillBuffer(index):
			# now buffer is filled, try again
			getNextRangesToProcess(ranges, index)
		# no more data for that chr in that buffer available
		else:
			return False
	return True;
###

# fill the ranges dictenary with data from all buffers
def initRanges(ranges):
	for index in buffers:
		getNextRangesToProcess(ranges, index)
###

# gets the borders and value of the next range to output
def getRangeToOutput(ranges):
	global lastPos, digitsToRound
	minHigh = -1

	for index in ranges:
		r = ranges[index]
		if minHigh == -1 or minHigh > r.end:
			minHigh = r.end

	# group the ranges
	value = groupRangesAndReload(ranges, lastPos, minHigh)
	range = Range(lastPos, minHigh, round(value, digitsToRound))
	# set the start of the next range to output
	lastPos = minHigh
	#print("	--> " + range.getOutputFormat(currentChr)) # DEBUG output
	return range
###

# groups the ranges in the dictionary, removes the fully processed ranges and reloads the next range if possible
def groupRangesAndReload(ranges, firstPos, lastPos):
	global removeRangesKeys, numberOfFiles
	#print("-------") # DEBUG output
	values = []
	# collect values and update ranges dictionary

	for index in ranges:
		# add the value
		r = ranges[index]
		#print(r.getOutputFormat(currentChr)) # DEBUG output
		# check, if the value can be added to the current interval
		if r.start <= firstPos and lastPos <= r.end:
			values.append(r.value)
		else:
			eprint("[ERROR] internal error: wrong shift of ranges! (%s %s %s)" %(currentChr, firstPos, lastPos))
			exit(1)

		# check, if that range is processed completely
		if r.end == lastPos:
			if getNextRangesToProcess(ranges, index) == False:
				removeRangesKeys.append(index)

	# remove ranges if no more range is there for the current chromosome
	while len(removeRangesKeys) > 0:
		index = removeRangesKeys.pop(0)
		ranges.pop(index, None)

	# group the values
	#print(values) # DEBUG output
	v = sum(values) / numberOfFiles
	return v
###

#######################################################
################# START OF MAIN PROGRAMM ##############
#######################################################

### get number of digits to round score
if args.numberOfDigits >= 0:
	digitsToRound = args.numberOfDigits

### get number of digits to round score
if args.normByReadCount <= 100000:
	eprint("A value for --normByReadCount smaller than 100000 seems not to be a meaningful choice. See --help for more details.")
	exit(1)

### ensure that there are at least two bedgraph files
if not len(args.bedgraphFiles) >= 1:
	eprint("At least two bedgraph files must be used as input. See --help for more details.")
	exit(1)
for bedgraphFile in args.bedgraphFiles:
	basename = os.path.basename(bedgraphFile)
	basename = os.path.splitext(basename)[0]
	keepSamples[basename] = 1

### read in the merged idxstats file
if os.path.isfile(args.mergedIdxstatsFile) and os.access(args.mergedIdxstatsFile, os.R_OK):
	with open(args.mergedIdxstatsFile, "r") as fh:
		print("idxstats file: %s" % (args.mergedIdxstatsFile))
		# skip head
		if not args.notSkipHead:
			fh.readline()
		for line in fh:
			tmp = line.strip().split("\t")
			sample = tmp[4]
			mapped = int(tmp[2])
			mappedTo = tmp[0]

			# only consider samples that should be merged
			if sample not in keepSamples:
				continue

			# update chr counter dict
			if mappedTo in chrsToProcess:
				chrsToProcess[mappedTo] += 1
			else:
				chrsToProcess[mappedTo] = 1
	#########################################################

	# drop all chrs that are not part of all samples
	delete = []
	N = len(args.bedgraphFiles)
	for key, mappedToCount in chrsToProcess.items():
		if mappedToCount != N:
			print("removing chr '%s' as it is only part of %i samples" % (key, mappedToCount))
			delete.append(key)

	# delete it from chrs that should be processed
	for key in delete:
		del(chrsToProcess[key])

	# reopen file but only count chrs that will be processed
	with open(args.mergedIdxstatsFile, "r") as fh:
		# skip head
		if not args.notSkipHead:
			fh.readline()
		for line in fh:
			tmp = line.strip().split("\t")
			sample = tmp[4]
			mapped = int(tmp[2])
			mappedTo = tmp[0]

			# only consider samples that should be merged
			if sample not in keepSamples:
				continue

			if mappedTo in chrsToProcess:
				# update dict
				if sample in scalingFactors:
					scalingFactors[sample] += mapped
				else:
					scalingFactors[sample] = mapped
	#########################################################
else:
	eprint("Merged Idxstats file '%s' was not found or is not readable." % (args.mergedIdxstatsFile))
	exit(1)

### norm the mapped read counts
for sample in scalingFactors:
	scalingFactors[sample] = scalingFactors[sample] / args.normByReadCount
	print("scaling factor for sample '%s' is %f" % (sample, scalingFactors[sample]))

# open all file handles of bedgraph files
index = 1
for bedgraphFile in args.bedgraphFiles:
	if os.path.isfile(bedgraphFile) and os.access(bedgraphFile, os.R_OK):
		fh = open(bedgraphFile, "r+")
		print("bedgraph file %i: %s" % (index, bedgraphFile))
		# store FH
		bedHandles[index] = fh
		buffers[index] = []
		buffersCurrentChr[index] = None
		bufferNoMoreData[index] = False

		# assign scaling factor to fh
		basename = os.path.basename(bedgraphFile)
		basename = os.path.splitext(basename)[0]
		if basename in scalingFactors:
			scalingFactors[index] = scalingFactors[basename]
		else:
			eprint("Scaling factor for sample with name '%s' is missing." % (basename))
			exit(1)

		# update the index for next file
		index = index+1
	else:
		eprint("File '%s' was not found or is not readable." % (bedgraphFile))
		exit(1)
numberOfFiles = len(bedHandles)

### check if base output folder exists
dirbase = os.path.dirname(args.outputFile)
if len(dirbase) > 0 and not os.path.exists(dirbase):
	os.makedirs(dirbase)

### open the output file and start with processing
with open(args.outputFile, "w") as outFH:
	print("output file: %s" % (args.outputFile))
	ranges = dict()
	lastPos = 0
	currentChr = ""
	while True:
		if len(ranges) > 0:
			outputRange = getRangeToOutput(ranges)
			# ommit zero ranges
			if outputRange.value != 0:
				outString = outputRange.getOutputFormat(currentChr)
				outFH.write(outString)
				outFH.write("\n")
		else:
			# end loop if no new chr could be loaded
			if processNextChrInFile(ranges):
				processedChrs.append(currentChr)
			else:
				break


# close the open file handles
for index in bedHandles:
	fh = bedHandles[index]
	fh.close()

# all was ok :)
print("Finished processing of %i chromosomes!" % (len(processedChrs)))
exit(0)