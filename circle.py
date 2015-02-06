from __future__ import print_function
from mpi4py import MPI
from globals import T, G
import logging
import random

logger = logging.getLogger("circle")

class Token:
    pass

class Circle:

    def __init__(self, name="Circle Work Comm",  split = "equal"):
        random.seed()  # use system time to seed
        logging_init()

        self.comm = MPI.COMM_WORLD.Dup()
        self.comm.Set_name(name)
        self.size = self.comm.Get_size()
        self.rank = self.comm.Get_rank()
        self.token_init()

        self.split = split
        self.reduce_outstanding = False
        self.request_outstanding = False
        self.task = None
        self.abort = False
        self.requestors = []
        self.workq = []

        # counters
        self.local_work_requested = 0
        self.local_work_processed = 0
        self.work_request_received = 0
        self.work_requested = False
        self.work_requested_rank = -1

        # barriers
        self.barrier_started = False

    def register(self, task):

        self.task = task

    def next_proc(self):
        """ Note next proc could return rank of itself """
        if self.size == 1:
            return MPI.PROC_NULL
        else:
            return random.randint(0, self.size-1)


    def token_status(self):
        return "rank: %s, token_src: %s, token_dest: %s, token_color: %s, token_proc: %s" % \
            (self.rank, self.token_src, self.token_dest, G.str[self.token_color], G.str[self.token_proc])

    def token_init(self):

        self.token_src = (self.rank - 1 + self.size) % self.size
        self.token_dest = (self.rank + 1 + self.size) % self.size
        self.token_color = G.NONE
        self.token_proc = G.WHITE
        self.token_is_local = False
        if self.rank == 0:
            self.token_is_local = True
            self.token_color = G.WHITE

        self.token_send_req = MPI.REQUEST_NULL


    def workq_status(self):
        return "rank %s has %s items in work queue" % (self.rank, len(self.workq))

    def begin(self):
        """ entry point to work """

        if self.rank == 0:
            self.task.create()
        logger.info(self.workq_status())
        # work until terminate
        self.loop()

        # check point?
        if self.abort:
            self.checkpoint()


    def checkpoint(self):
        pass

    def enq(self, work):
        self.workq.append(work)

    def deq(self):
        if len(self.workq) > 0:
            return self.workq.pop(0)
        else:
            return None

    def check_reduce(self):
        pass

    def barrier_start(self):
        self.barrier_started = True

    def barrier_test(self):

        # barrier has not been started
        if not self.barrier_started:
            return False

        # FIXME
        # do we really need async barrier?
        self.comm.Barrier()

    def bcase_abort(self):
        self.abort = True
        buf = G.ABORT
        for i in range(self.size):
            if (i != self.rank):
                self.comm.send(buf, i, tag = T.WORK_REQUEST)
                logger.warn("abort message sent to %s" % i)

    def loop(self):
        """ central loop to finish the work """
        while True:
            # check for and service requests
            self.request_check()

            if len(self.workq) == 0:
                logger.info("rank %s have no work, issue request work" % self.rank)
                self.request_work()

            # if I have work, and no abort signal, process one
            if len(self.workq) > 0 and not self.abort:
                self.task.process()
                self.local_work_processed += 1

            else:
                status = self.check_for_term();
                if status == G.TERMINATE:
                    break;
        #
        # We got here because
        # (1) all processes finish the work
        # (2) abort
        #

        logger.debug("All process finished or ready to abort")

        self.comm.Barrier()

    def cleanup(self):
        while True:
            if not (self.reduce_outstanding or self.request_outstanding) and \
                self.token_send_req == MPI.REQUEST_NULL:
                self.barrier_start()

            # break the loop when non-blocking barrier completes
            if self.barrier_test():
                break

            # send no work message for any work request that comes in
            self.request_check()

            # clean up any outstanding reduction
            self.reduce_check()

            # recv any incoming work reply messages
            self.request_work()

            # check and recv any incoming token
            self.token_check()

            # if we have an outstanding token, check if it has been recv'ed
            # FIXME
            if self.token_send_req != MPI.REQUEST_NULL:
                self.token_send_req.Test()


    def check_for_term(self):

        if self.token_proc == G.TERMINATE:
            return G.TERMINATE

        if self.size == 1:
            self.token_proc = G.TERMINATE
            return G.TERMINATE

        if self.token_is_local:
            # we have no work, but we have token
            if self.rank == 0:
                # rank 0 start with white token
                self.token_color = G.WHITE
            elif self.token_proc == G.BLACK:
                # others turn the token black
                # if they are in black state
                self.token_color = G.BLACK

            self.token_issend()

            # flip process color from black to white
            self.token_proc = G.WHITE
        else:
            # we have no work, but we don't have the token
            self.token_check()

        # return current status
        return self.token_proc

    def request_check(self):
        buf = None
        while True:
            status = MPI.Status()
            ret = self.comm.Iprobe(MPI.ANY_SOURCE, T.WORK_REQUEST, status)
            if not ret: break
            # we have work request message
            rank = status.Get_source()
            self.comm.recv(buf, rank, T.WORK_REQUEST, status)
            if buf == G.ABORT:
                self.abort = True
                logger.info("Abort request recv'ed")
                return False
            else:
                logger.info("requestors: %s, buf = %s" % (rank, buf))
                # add rank to requesters
                self.requestors.append(rank)

        # we don't have any request

        if len(self.requestors) == 0:
            return False
        else:
            logger.debug("rank %s have %s requesters, with %s work in queue" %
                         (self.rank, len(self.requestors), self.workq))
            # have work requesters
            if len(self.workq) == 0:
                for rank in self.requestors:
                    self.send_no_work(rank)
            else:
                # we do have work
                self.send_work_to_many()


    def send_no_work(self, rank):
        """ send no work reply to someone requesting work"""

        buf = G.ABORT if self.abort else G.ZERO
        logger.info("sending no work reply: %s" % buf)
        self.comm.send(buf, dest = rank, tag = T.WORK_REPLY)

    def spread_counts(self, rcount, wcount):
        """ Given wcount work items and rcount requesters
            spread it evenly among all requesters
        """

        base = wcount / rcount
        extra = wcount - (base * rcount)
        sizes = [base] * rcount
        for i in range(extra):
            sizes[i] += 1
        return sizes

    def send_work_to_many(self):
        rcount = len(self.requestors)
        wcount = len(self.workq)
        sizes = None
        if self.split == "equal":
            sizes = self.spread_counts(rcount, wcount)
        else:
            raise NotImplementedError

        logger.debug("requester count: %s, work count: %s, spread: %s" % (rcount, wcount, sizes))
        for idx, dest in enumerate(self.requestors):
            self.send_work(dest, sizes[idx])


    def send_work(self, dest, count):
        """ dest is the rank of requester, count is the number of work to send
        """
        # for termination detection
        if (dest < self.rank) or (dest == self.token_src):
            self.token_proc = G.BLACK

        # first message, send # of work items
        self.comm.send(count, dest, T.WORK_REPLY)

        # second message, actual work items
        self.comm.send(self.workq[0:count-1], dest, T.WORK_REPLY)
        logger.debug("%s work items sent to rank %s" % (count, dest))

        # remove work items
        del self.workq[0:count-1]

    def request_work(self, cleanup = False):

        status = MPI.Status()
        # check if we have request outstanding
        if self.work_requested:
            logger.debug("rank %s have work requested from rank %s, check reply"
                         % (self.rank, self.work_requested_rank))
            source = self.work_requested_rank
            # do we got a reply?
            ret = self.comm.Iprobe(source, T.WORK_REPLY, status)
            if ret:
                self.workreq_receive(source)
                # flip flag to indicate we no longer waiting for reply
                self.work_requested = False
        elif not cleanup:
            # send request
            dest = self.next_proc()
            if dest == MPI.PROC_NULL:
                # have no one to ask, we are done
                return False
            logger.debug("send work request to %s" % dest)
            self.local_work_requested += 1
            buf = G.ABORT if self.abort else G.NORMAL

            # blocking send
            self.comm.send(buf, dest, T.WORK_REQUEST)
            self.work_requested = True
            self.work_requested_rank = dest

    def workreq_receive(self, source):
        """ when incoming work request detected """

        buf = None
        # first message, check normal or abort
        self.comm.recv(buf, source, T.WORK_REPLY)
        logger.info("rank %s receive work from rank %s, first msg: %s"
                    % (self.rank, source, buf))

        if buf == G.ABORT:
            logger.debug("receive abort signal")
            self.abort = True
            return G.ABORT
        elif buf == G.ZERO:
            logger.debug("receive zero signal")
            # no follow up message, return
            return G.ZERO
        else:
            pass

        # second message, the actual work itmes
        self.comm.recv(buf, source, T.WORK_REPLY)
        logger.info("rank %s receive work from rank %s, second msg:  %s"
                    % (self.rank, source, buf))
        if buf is None:
            raise RuntimeError
        else:
            self.workq.append(buf)

    def finalize(self):
        """ clean up """
        pass
    # TOKEN MANAGEMENT
    def token_recv(self):
        # verify we don't have a local token
        if self.token_is_local:
            raise RuntimeError("token_is_local True")

        # this won't block as token is waiting
        buf = None
        self.comm.recv(buf, self.token_src, T.TOKEN)
        if buf is None:
            raise RuntimeError("token color is None")
        self.token_color = buf

        # record token is local
        self.token_is_local = True

        # if we have a token outstanding, at this point
        # we should have received the reply (even if we
        # sent the token to ourself, we just replied above
        # so the send should now complete
        #
        if self.token_send_req != MPI.PROC_NULL:
            pass


        # now set our state
        if self.token_proc == G.BLACK:
            self.token_color = G.BLACK
            self.token_proc = G.WHITE

        # check for terminate condition
        terminate = False

        if self.rank == 0 and self.token_color == G.WHITE:
            # if rank 0 receive a white token
            logger.info("Master detected termination")
            terminate = True
        elif self.token_color == G.TERMINATE:
            terminate = True

        # forward termination token if we have one
        if terminate:
            # send terminate token, don't bother
            # if we the last rank
            self.token_color = G.TERMINATE
            if self.rank < self.size -1:
                self.token_issend()

            # set our state to terminate
            self.token_proc = G.TERMINATE

    def token_check(self):
        """
        check for token, and receive it if arrived
        """

        status = MPI.Status()
        flag = self.comm.Iprobe(self.token_src, T.TOKEN, status)
        if flag:
            self.token_recv()

    def token_issend(self):
        # don't send if abort
        if self.abort: return
        logger.debug("token send: token_color = %s" % self.token_color)
        self.comm.send(self.token_color,
            self.token_dest, tag = T.TOKEN)

        # now we don't have the token
        self.token_is_local = False

    def reduce(self, count):
        pass

    def reduce_check(self):
        pass


    def set_loglevel(self, level):
        global logger
        logger.setLevel(level)

def logging_init(level=logging.INFO):

    global logger
    fmt = logging.Formatter(G.simple_fmt)
    logger.setLevel(level)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)
    logger.propagate = False

