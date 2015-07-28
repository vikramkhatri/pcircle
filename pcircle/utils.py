from __future__ import print_function
import sys
import time
import itertools
import logging
import re
import os.path

from pcircle.globals import G

__author__ = 'Feiyi Wang'

def numeric_level(loglevel):

    level = getattr(logging, loglevel.upper(), None)
    if not isinstance(level, int):
        raise ValueError("Invalid log level: %s" % loglevel)
    return level


def getLogger(name, level, logfile=None):
    """
    :param name: logger name
    :param level: string value e.g. "error"
    :param logfile: destination file, optional
    :return: logger
    """
    simple_fmt = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    logger = logging.getLogger(name)
    ll = numeric_level(level)
    logger.setLevel(ll)
    # console handler
    console = logging.StreamHandler()
    console.setLevel(logging.ERROR)  #
    console.setFormatter(logging.Formatter(simple_fmt))
    logger.addHandler(console)
    if logfile:
        # file handler, honor request
        fh = logging.FileHandler(logfile, mode="a")
        fh.setLevel(ll)
        fh.setFormatter(logging.Formatter(simple_fmt))
        logger.addHandler(fh)

    return logger

def destpath(srcdir, destdir, srcfile):
    """
    srcdir -> source path
    destdir -> destination path
    srcfile -> full source file path
    return the destination file path
    """
    destbase = os.path.basename(destdir)
    rpath = os.path.relpath(srcfile, start=srcdir)

    if rpath == ".":
        return destdir
    else:
        return destdir + "/" + rpath

def conv_unit(s):
    " convert a unit to number"
    d = { "B": 1,
         "K": 1024,
         "M": 1024*1024,
         "G": 1024*1024*1024,
         "T": 1024*1024*1024*1024}
    s = s.upper()
    match = re.match(r"(\d+)(\w+)", s, re.I)
    if match:
        items = match.groups()
        v = int(items[0])
        u = items[1]
        return v * d[u]

    raise ValueError("Can't convert %s" % s)

def bytes_fmt(n):
    d = {'1mb': 1048576,
         '1gb': 1073741824,
         '1tb': 1099511627776}
    if n < d['1mb']:
        return "%.2f KiB" % (float(n)/1024)

    if n < d['1gb']:
        return "%.2f MiB" % (float(n)/d['1mb'])

    if n < d['1tb']:
        return "%.2f GiB" % (float(n)/d['1gb'])

    return "%.2f TiB" % (float(n)/d['1tb'])


# SO: http://stackoverflow.com/questions/13520622/python-script-to-show-progress
def spiner():
    for c in itertools.cycle('|/-\\'):
        sys.stdout.write('\r' + c)
        sys.stdout.flush()
        time.sleep(0.2)

# SO: http://stackoverflow.com/questions/3002085/python-to-print-out-status-bar-and-percentage

def progress():
    import sys
    total = 10000000
    point = total / 100
    increment = total / 20
    for i in xrange(total):
        if(i % (5 * point) == 0):
            sys.stdout.write("\r[" + "=" * (i / increment) +  " " * ((total - i)/ increment) + "]" +  str(i / point) + "%")
            sys.stdout.flush()


class bcolors:
    """
    Black       0;30     Dark Gray     1;30
    Blue        0;34     Light Blue    1;34
    Green       0;32     Light Green   1;32
    Cyan        0;36     Light Cyan    1;36
    Red         0;31     Light Red     1;31
    Purple      0;35     Light Purple  1;35
    Brown       0;33     Yellow        1;33
    Light Gray  0;37     White         1;37
    """

    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKGREEN = '\033[92m'
    INFO = '\033[1;33m'  # yellow
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'

    def disable(self):
        self.HEADER = ''
        self.OKBLUE = ''
        self.OKGREEN = ''
        self.WARNING = ''
        self.FAIL = ''
        self.ENDC = ''

def hprint(msg):
    print(bcolors.INFO + msg + bcolors.ENDC)

def eprint(msg):
    print(bcolors.FAIL + msg + bcolors.ENDC)

def timestamp():
    import time
    return time.strftime("%Y.%m.%d.%H%M%S")


def timestamp2():
    import time
    return time.strftime("%Y-%m-%d-%H%M%S")


def breakline(line, size=60, minsz=10):
    ret = ''
    total = len(line)
    if total <= size:
        return line

    while total > size:
        ret += line[0:size]
        ret += ' \ \n    '
        total -= size
        line = line[size:]

    if total < minsz:
        return ret[:-8] + line[:]
    else:
        return ret + line[:]

def print_cmdline():
    cmdline = " ".join(sys.argv[:])
    print("Command Line:\t", breakline(cmdline), "\n")