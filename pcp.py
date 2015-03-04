#!/usr/bin/env python
from __future__ import print_function

from mpi4py import MPI
import stat
import os
import os.path
import logging
import argparse
import utils
import hashlib
import sys
import signal
import cPickle as pickle

from collections import Counter, defaultdict
from utils import bytes_fmt, destpath
from lru import LRU
from threading import Thread

from task import BaseTask
from pcheck import PCheck
from circle import Circle
from globals import G
from pwalk import PWalk
from checkpoint import Checkpoint


ARGS = None
logger = logging.getLogger("pcp")
circle = None

def parse_args():
    parser = argparse.ArgumentParser(description="A MPI-based Parallel Copy Tool")
    parser.add_argument("--loglevel", default="ERROR", help="log level")
    parser.add_argument("--chunksize", default="1m", help="chunk size")
    parser.add_argument("--reduce-interval", type=int, default=10, help="interval")
    parser.add_argument("--checkpoint-interval", type=int, default=360, help="checkpoint interval")
    parser.add_argument("--checkpoint-id", default=None, help="checkpoint id")
    parser.add_argument("-c", "--checksum", action="store_true", help="verify")
    parser.add_argument("-p", "--preserve", action="store_true", help="preserve meta info")
    parser.add_argument("src", help="copy from")
    parser.add_argument("dest", help="copy to")

    return parser.parse_args()

def sig_handler(signal, frame):
    # catch keyboard, do nothing
    # eprint("\tUser cancelled ... cleaning up")
    sys.exit(1)

class PCP(BaseTask):
    def __init__(self, circle, src, dest,
                 treewalk = None,
                 totalsize=0,
                 checksum=False,
                 climit=0,
                 prune=False,
                 workq=None):
        BaseTask.__init__(self, circle)
        self.circle = circle
        self.treewalk = treewalk
        self.totalsize = totalsize
        self.prune = prune
        self.workq = workq

        self.checkpoint_file = None

        self.src = os.path.abspath(src)
        self.srcbase = os.path.basename(src)
        self.dest = os.path.abspath(dest)

        # cache, keep the size conservative
        self.cache_limit = climit
        if  climit == 0:
            self.cache_limit = 1024/circle.size - 16

        self.rfd_cache = LRU(self.cache_limit)
        self.wfd_cache = LRU(self.cache_limit)

        self.cnt_filesize_prior = 0
        self.cnt_filesize = 0

        self.blocksize = 1024*1024
        self.chunksize = 1024*1024


        # debug
        self.d = {"rank": "rank %s" % circle.rank}
        self.wtime_started = MPI.Wtime()
        self.wtime_ended = None
        self.workcnt = 0
        if self.treewalk:
            logger.debug("treewalk files = %s" % treewalk.flist, extra=self.d)

        # fini_check
        self.fini_cnt = Counter()

        # checksum
        self.checksum = defaultdict(list)

        # checkpointing
        self.checkpoint_interval = sys.maxsize
        self.checkpoint_last = MPI.Wtime()

        if self.circle.rank == 0:
            print("Start copying process ...")

    def set_chunksize(self, sz):
        self.chunksize = sz

    def set_checkpoint_interval(self, interval):
        self.checkpoint_interval = interval

    def set_checkpoint_file(self, f):
        self.checkpoint_file = f

    def cleanup(self):
        for f in self.rfd_cache.values():
            os.close(f)

        for f in self.wfd_cache.values():
            os.close(f)

        # remove checkpoint file
        if os.path.exists(self.checkpoint_file):
            os.remove(self.checkpoint_file)

    def enq_dir(self, f):
        """ Deprecated, should not be in use anymore """
        d = {}
        d['cmd'] = "mkdir"
        d['src'] = f[0]
        d['dest'] = self.dest + "/" + self.srcbase + "/" + os.path.relpath(f[0], start=self.src)
        self.enq(d)

    def enq_file(self, f):
        '''
        f[0] path f[1] mode f[2] size - we enq all in one shot
        CMD = copy src  dest  off_start  last_chunk
        '''
        chunks    = f[2] / self.chunksize
        remaining = f[2] % self.chunksize

        d = {}
        d['cmd'] = 'copy'
        d['src'] = f[0]
        d['dest'] = destpath(self.src, self.dest, f[0])

        workcnt = 0

        if f[2] == 0:
            # empty file
            d['off_start'] = 0
            d['length'] = 0
            self.enq(d)
            logger.debug("%s" % d, extra=self.d)
            workcnt += 1
        else:
            for i in range(chunks):
                d['off_start'] = i * self.chunksize
                d['length'] = self.chunksize
                self.enq(d)
                logger.debug("%s" % d, extra=self.d)
            workcnt += chunks

        if remaining > 0:
            # send remainder
            d['off_start'] = chunks * self.chunksize
            d['length' ] = remaining
            self.enq(d)
            logger.debug("%s" % d, extra=self.d)
            workcnt += 1

        # tally work cnt
        self.workcnt += workcnt

        # --------------------------------------
        # TODO: make finish token for this file
        # --------------------------------------
        # t = {'cmd' : 'fini_check', 'workcnt' : workcnt, 'src' : f[0], 'dest': d['dest'] }
        # self.enq(t)


    def create(self):
        if self.workq:  # restart
            self.setq(self.workq)
            return

        # construct and enable all copy operations
        for f in self.treewalk.flist:
            if stat.S_ISREG(f[1]):
                self.enq_file(f)

        # right after this, we do first checkpoint
        if self.checkpoint_file:
            self.do_no_interrupt_checkpoint()
            self.checkpoint_last = MPI.Wtime()

    def do_open(self, k, d, flag):
        """
        :param k: key
        :param d: dict
        :return: file handle
        """
        if d.has_key(k):
            return d[k]

        if len(d.keys()) < self.cache_limit:
            fd = os.open(k, flag)
            if fd > 0: d[k] = fd
            return fd
        else:
            # over the limit
            # clean up the least used
            old_k, old_v = d.items()[-1]
            logger.debug("Closing fd for %s" % old_k, extra=self.d)
            os.close(old_v)

            fd = os.open(k, flag)
            if fd > 0: d[k] = fd
            return fd

    def do_mkdir(self, work):
        src = work['src']
        dest = work['dest']
        if not os.path.exists(dest):
            os.makedirs(dest)

    def do_copy(self, work):
        src = work['src']
        dest = work['dest']

        basedir = os.path.dirname(dest)
        if not os.path.exists(basedir):
            os.makedirs(basedir)

        rfd = self.do_open(src, self.rfd_cache, os.O_RDONLY)
        wfd = self.do_open(dest, self.wfd_cache, os.O_WRONLY | os.O_CREAT)
        if wfd < 0:
            if ARGS.force:
                os.unlink(dest)
                wfd = self.do_open(dest, self.wfd_cache, os.O_WRONLY)
            else:
                logger.error("Failed to create output file %s" % dest, extra=self.d)
                return

        # do the actual copy
        self.write_bytes(rfd, wfd, work)

        # update tally
        self.cnt_filesize += work['length']
        logger.debug("transfer bytes %s" % self.cnt_filesize, extra=self.d)

        #self.fini_cnt[src] += 1
        #logger.debug("Inc workcnt for %s (workcnt=%s)" % (src, self.fini_cnt[src]), extra=self.d)

    def do_fini_check(self, work):
        src = work['src']
        dest = work['dest']
        workcnt = work['workcnt']

        mycnt = self.fini_cnt[src]
        if workcnt == mycnt:
            # all job finished
            # we should have cached this before
            rfd = self.rfd_cache[src]
            wfd = self.wfd_cache[dest]
            os.close(rfd)
            os.close(wfd)
            del self.rfd_cache[src]
            del self.wfd_cache[dest]
            logger.debug("Finish done: %s" % src, extra=self.d)
        elif mycnt < workcnt:
            # worked some, but not yet done
            work['workcnt'] -= mycnt
            self.enq(work)
            logger.debug("Finish enq: %s (workcnt=%s) " % (src, work['workcnt']), extra=self.d)
        # either way, clear the cnt
        del self.fini_cnt[src]


    def do_no_interrupt_checkpoint(self):
        a = Thread(target=self.do_checkpoint)
        a.start()
        a.join()
        logger.debug("checkpoint: %s" % self.checkpoint_file, extra=self.d )

    def do_checkpoint(self):
        for k in self.wfd_cache.keys():
            os.close(self.wfd_cache[k])

        # clear the cache
        self.wfd_cache.clear()

        tmp_file = self.checkpoint_file + ".part"
        with open(tmp_file, "wb") as f:
            cobj = Checkpoint(self.src, self.dest, self.get_workq(), self.totalsize)
            pickle.dump(cobj, f, pickle.HIGHEST_PROTOCOL)
        # POSIX requires rename to be atomic
        os.rename(tmp_file, self.checkpoint_file)

    def process(self):
        """
        The only work is "copy", TODO: clean up other actions such as mkdir/fini_check
        """
        curtime = MPI.Wtime()
        if curtime - self.checkpoint_last > self.checkpoint_interval:
            self.do_no_interrupt_checkpoint()
            self.checkpoint_last = curtime

        work = self.deq()
        if work['cmd'] == 'copy':
            self.do_copy(work)
        elif work['cmd'] == 'mkdir':
            # self.do_mkdir(work)
            pass
        elif work['cmd'] == 'fini_check':
            # self.do_fini_check(work)
            pass
        else:
            logger.error("Unknown command %s" % work['cmd'], extra=self.d)
            self.abort()

    def reduce_init(self, buf):
        buf['cnt_filesize'] = self.cnt_filesize


    def reduce(self, buf1, buf2):
        buf1['cnt_filesize'] += buf2['cnt_filesize']
        return buf1

    def reduce_report(self, buf):
        out = ""
        if self.totalsize != 0:
            out += "%.2f %% finished, " % (100* float(buf['cnt_filesize']) / self.totalsize)

        out += "%s copied" % bytes_fmt(buf['cnt_filesize'])

        if self.circle.reduce_time_interval != 0:
            rate = float(buf['cnt_filesize'] - self.cnt_filesize_prior) / self.circle.reduce_time_interval
            self.cnt_filesize_prior = buf['cnt_filesize']
            out += ", estimated transfer rate: %s/s" % bytes_fmt(rate)

        print(out)

    def reduce_finish(self, buf):
        #self.reduce_report(buf)
        pass

    def epilogue(self):
        self.wtime_ended = MPI.Wtime()
        if self.circle.rank == 0:
            print("")
            if self.totalsize == 0: return
            time = self.wtime_ended - self.wtime_started
            rate = float(self.totalsize)/time
            print("Copy Job Completed In: %.2f seconds" % (time))
            print("Average Transfer Rate: %s/s\n" % bytes_fmt(rate))


    def write_bytes(self, rfd, wfd, work):
        os.lseek(rfd, work['off_start'], os.SEEK_SET)
        os.lseek(wfd, work['off_start'], os.SEEK_SET)

        # FIXME: assumed read will return what we asked for
        # in C, this is not the case though. A loop check
        # might be needed.
        buf = os.read(rfd, work['length'])
        assert len(buf) == work['length']
        os.write(wfd, buf)

        # are we doing checksum?
        if self.checksum:
            digest = hashlib.md5(buf).hexdigest()
            self.checksum[work['dest']].append((work['off_start'], work['length'], digest, work['src']))


def verify_path(circ, isrc, idest):
    """ verify and return target destination"""

    if not os.path.exists(isrc) or not os.access(isrc, os.R_OK):
        if circ.rank == 0: print("source directory %s is not readable" % isrc)
        circ.exit(0)

    srcbase = os.path.basename(isrc)
    odest = idest + "/" + srcbase

    if os.path.exists(odest):
        if circ.rank == 0: print("Destination [%s] exists, will not overwrite!" % odest)
        circ.exit(0)

    if os.path.exists(idest) and os.access(idest, os.W_OK):
        #os.mkdir(odest)
        return odest

    parent_dir = os.path.dirname(idest)

    if not os.path.exists(idest) and os.access(parent_dir, os.W_OK):
        #os.mkdir(idest)
        return idest
    else:
        if circ.rank == 0: print("Error: destination %s is not accessible" % idest)
        circ.exit(0)

    # should not come to this point
    raise

def main():

    global ARGS, logger, circle
    signal.signal(signal.SIGINT, sig_handler)


    ARGS = parse_args()

    circle = Circle(reduce_interval=ARGS.reduce_interval)
    circle.setLevel(logging.ERROR)

    logger = utils.logging_init(logger, ARGS.loglevel)

    src = os.path.abspath(ARGS.src)
    dest = os.path.abspath(ARGS.dest)
    dest = verify_path(circle, src, dest)

    # first task
    treewalk = PWalk(circle, src, dest, preserve = ARGS.preserve)
    treewalk.set_loglevel(ARGS.loglevel)
    circle.begin(treewalk)
    circle.finalize(reduce_interval=ARGS.reduce_interval)
    tsz = treewalk.epilogue()

    # second task
    pcp = PCP(circle, src, dest, treewalk = treewalk,
              totalsize=tsz, checksum=ARGS.checksum)

    pcp.set_chunksize(utils.conv_unit(ARGS.chunksize))
    pcp.set_checkpoint_interval(ARGS.checkpoint_interval)
    if ARGS.checkpoint_id:
        pcp.set_checkpoint_file(".pcp_workq.%s.%s" % (ARGS.checkpoint_id, circle.rank))
    else:
        ts = utils.timestamp()
        circle.comm.bcast(ts)
        pcp.set_checkpoint_file(".pcp_workq.%s.%s" % (ts, circle.rank))

    circle.begin(pcp)
    circle.finalize(reduce_interval=ARGS.reduce_interval)
    pcp.cleanup()


    # third task
    if ARGS.checksum:
        pcheck = PCheck(circle, pcp, tsz)
        pcheck.setLevel(ARGS.loglevel)
        circle.begin(pcheck)
        circle.finalize()

        tally = pcheck.fail_tally()

        if circle.rank == 0:
            print("")
            if tally == 0:
                print("Verification passed!")
            else:
                print("Verification failed")

    pcp.epilogue()

if __name__ == "__main__": main()

