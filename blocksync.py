#!/usr/bin/env python2
"""
Synchronise block devices over the network

Copyright 2006-2008 Justin Azoff <justin@bouncybouncy.net>
Copyright 2011 Robert Coup <robert@coup.net.nz>
Copyright 2012 Holger Ernst <info@ernstdatenmedien.de>
Copyright 2014 Robert McQueen <robert.mcqueen@collabora.co.uk>
Copyright 2016 Theodor-Iulian Ciobanu
License: GPL

Getting started:

* Copy blocksync.py to the home directory on the remote host & make it executable
* Make sure your remote user is either root or can sudo (use -s for sudo)
* Make sure your local user can ssh to the remote host (use -i for a SSH key)
* Invoke:
    python blocksync.py /dev/source [user@]remotehost [/dev/dest]

* Specify localhost for local usage:
    python blocksync.py /dev/source localhost /dev/dest
"""

import os
import sys
from hashlib import sha512, sha384, sha1, md5
from math import ceil
import subprocess
import time
from datetime import timedelta

SAME = "0"
DIFF = "1"
COMPLEN = len(SAME)  # SAME/DIFF length


def do_create(f, size):
    f = open(f, 'a')
    f.truncate(size)
    f.close()


def do_open(f, mode):
    f = open(f, mode)
    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    return f, size


def getblocks(f, blocksize):
    while 1:
        block = f.read(blocksize)
        if not block:
            break
        yield block


def server(dev, deleteonexit, options):
    blocksize, addhash = options.blocksize, options.addhash

    if options.weakhash:
        hash1 = sha1
        hash2 = md5
    else:
        hash1 = sha512
        hash2 = sha384

    print 'init'
    sys.stdout.flush()

    size = int(sys.stdin.readline().strip())
    if size > 0:
        do_create(dev, size)

    print dev, blocksize
    f, size = do_open(dev, 'r+')
    print size
    sys.stdout.flush()

    startpos = int(sys.stdin.readline().strip())
    maxblock = int(sys.stdin.readline().strip()) - 1

    f.seek(startpos)

    for i, block in enumerate(getblocks(f, blocksize)):
        sys.stdout.write(hash1(block).digest())
        if addhash:
            sys.stdout.write(hash2(block).digest())
        sys.stdout.flush()
        res = sys.stdin.read(COMPLEN)
        if res == DIFF:
            newblock = sys.stdin.read(blocksize)
            f.seek(-len(newblock), 1)
            f.write(newblock)
        if i == maxblock:
            break

    if deleteonexit:
        os.remove(__file__)


def copy_self(workerid, remotecmd):
    with open(__file__) as srcfile:
        cmd = remotecmd + ['/usr/bin/env', 'sh', '-c', '"SCRIPTNAME=\`mktemp -q\`; cat >\$SCRIPTNAME; echo \$SCRIPTNAME"', '<<EOT\n', srcfile.read(), '\nEOT']

    p = subprocess.Popen(cmd, bufsize=0, stdin=subprocess.PIPE, stdout=subprocess.PIPE, close_fds=True)
    p_in, p_out = p.stdin, p.stdout

    remotescript = p_out.readline().strip()
    p.poll()
    if p.returncode is not None:
        print "[worker %d] Error copying blocksync to the remote host!" % (workerid)
        sys.exit(1)

    return remotescript


def sync(workerid, srcdev, dsthost, dstdev, options):
    blocksize = options.blocksize
    addhash = options.addhash
    dryrun = options.dryrun
    interval = options.interval

    if not dstdev:
        dstdev = srcdev

    if options.weakhash:
        hash1 = sha1
        hash2 = md5
    else:
        hash1 = sha512
        hash2 = sha384
    hash1len = hash1().digestsize
    hash2len = hash2().digestsize

    print "Starting worker #%d (pid: %d)" % (workerid, os.getpid())
    print "[worker %d] Block size is %0.1f MB" % (workerid, blocksize / (1024.0 * 1024))

    try:
        f, size = do_open(srcdev, 'r')
    except Exception, e:
        print "[worker %d] Error accessing source device! %s" % (workerid, e)
        sys.exit(1)

    chunksize = int(size / options.workers)
    startpos = workerid * chunksize
    if workerid == (options.workers - 1):
        chunksize += size - (chunksize * options.workers)
    print "[worker %d] Chunk size is %0.1f MB, offset is %d" % (workerid, chunksize / (1024.0 * 1024), startpos)

    pause_ms = 0
    if options.pause:
        # sleep() wants seconds...
        pause_ms = options.pause / 1000.0
        print "[worker %d] Slowing down for %d ms/block (%0.4f sec/block)" % (workerid, options.pause, pause_ms)

    cmd = []
    if dsthost != 'localhost':
        if options.passenv:
            cmd += ['/usr/bin/env', 'SSHPASS=%s' % (os.environ[options.passenv]), 'sshpass', '-e']
        cmd += ['ssh', '-c', options.cipher]
        if options.keyfile:
            cmd += ['-i', options.keyfile]
        if options.compress:
            cmd += ['-C']
        cmd += [dsthost]
    if options.sudo:
        cmd += ['sudo']

    if options.script:
        servercmd = 'server'
        remotescript = options.script
    elif (dsthost =='localhost'):
        servercmd = 'server'
        remotescript = __file__
    else:
        servercmd = 'tmpserver'
        remotescript = copy_self(workerid, cmd)

    cmd += [options.interpreter, remotescript, servercmd, dstdev, '-b', str(blocksize)]

    if addhash:
        cmd += ['-2']

    if options.weakhash:
        cmd += ['-W']

    print "[worker %d] Running: %s" % (workerid, " ".join(cmd[2 if options.passenv and (dsthost != 'localhost') else 0:]))

    p = subprocess.Popen(cmd, bufsize=0, stdin=subprocess.PIPE, stdout=subprocess.PIPE, close_fds=True)
    p_in, p_out = p.stdin, p.stdout

    line = p_out.readline()
    p.poll()
    if (p.returncode is not None) or (line.strip() != 'init'):
        print "[worker %d] Error connecting to or invoking blocksync on the remote host!" % (workerid)
        sys.exit(1)

    p_in.write("%d\n" % (size if options.createdest else 0))
    p_in.flush()

    line = p_out.readline()
    p.poll()
    if p.returncode is not None:
      print "[worker %d] Failed creating destination file on the remote host!" % (workerid)
      sys.exit(1)

    a, b = line.split()
    if a != dstdev:
        print "[worker %d] Dest device (%s) doesn't match with the remote host (%s)!" % (workerid, dstdev, a)
        sys.exit(1)
    if int(b) != blocksize:
        print "[worker %d] Source block size (%d) doesn't match with the remote host (%d)!" % (workerid, blocksize, int(b))
        sys.exit(1)

    line = p_out.readline()
    p.poll()
    if p.returncode is not None:
        print "[worker %d] Error accessing device on remote host!" % (workerid)
        sys.exit(1)
    remote_size = int(line)
    if size > remote_size:
        print "[worker %d] Source device size (%d) doesn't fit into remote device size (%d)!" % (workerid, size, remote_size)
        sys.exit(1)
    elif size < remote_size:
        print "[worker %d] Source device size (%d) is smaller than remote device size (%d), proceeding anyway" % (workerid, size, remote_size)

    same_blocks = diff_blocks = last_blocks = 0
    interactive = os.isatty(sys.stdout.fileno())

    t0 = time.time()
    t_last = t0
    f.seek(startpos)
    size_blocks = ceil(chunksize / float(blocksize))
    p_in.write("%d\n%d\n" % (startpos, size_blocks))
    p_in.flush()
    print "[worker %d] Start syncing %d blocks..." % (workerid, size_blocks)
    for l_block in getblocks(f, blocksize):
        l1_sum = hash1(l_block).digest()
        r1_sum = p_out.read(hash1len)
        if addhash:
            l2_sum = hash2(l_block).digest()
            r2_sum = p_out.read(hash2len)
            r2_match = (l2_sum == r2_sum)
        else:
            r2_match = True
        if (l1_sum == r1_sum) and r2_match:
            same_blocks += 1
            p_in.write(SAME)
            p_in.flush()
        else:
            diff_blocks += 1
            if dryrun:
                p_in.write(SAME)
                p_in.flush()
            else:
                p_in.write(DIFF)
                p_in.flush()
                p_in.write(l_block)
                p_in.flush()

        if pause_ms:
            time.sleep(pause_ms)

        if not interactive:
            continue

        t1 = float(time.time())
        if (t1 - t_last) >= interval:
            done_blocks = same_blocks + diff_blocks
            delta_blocks = done_blocks - last_blocks
            rate = delta_blocks * blocksize / (1024 * 1024 * (t1 - t_last))
            print "[worker %d] same: %d, diff: %d, %d/%d, %5.1f MB/s (%s remaining)" % (workerid, same_blocks, diff_blocks, done_blocks, size_blocks, rate, timedelta(seconds = ceil((size_blocks - done_blocks) * (t1 - t0) / done_blocks)))
            last_blocks = done_blocks
            t_last = t1

        if (same_blocks + diff_blocks) == size_blocks:
            break

    rate = size_blocks * blocksize / (1024.0 * 1024) / (time.time() - t0)
    print "[worker %d] same: %d, diff: %d, %d/%d, %5.1f MB/s" % (workerid, same_blocks, diff_blocks, same_blocks + diff_blocks, size_blocks, rate)

    print "[worker %d] Completed in %s" % (workerid, timedelta(seconds = ceil(time.time() - t0)))

    return same_blocks, diff_blocks

if __name__ == "__main__":
    from optparse import OptionParser, SUPPRESS_HELP
    parser = OptionParser(usage = "%prog [options] /dev/source [user@]remotehost [/dev/dest]")
    parser.add_option("-w", "--workers", dest = "workers", type = "int", help = "number of workers to fork (defaults to 1)", default = 1)
    parser.add_option("-b", "--blocksize", dest = "blocksize", type = "int", help = "block size (bytes, defaults to 1MB)", default = 1024 * 1024)
    parser.add_option("-2", "--additionalhash", dest = "addhash", action = "store_true", help = "use two message digests when comparing blocks", default = False)
    parser.add_option("-W", "--weakhash", dest = "weakhash", action = "store_true", help = "use weaker but faster message digests (SHA1[+MD5] instead of SHA512[+SHA384])", default = False)
    parser.add_option("-p", "--pause", dest = "pause", type="int", help = "pause between processing blocks, reduces system load (ms, defaults to 0)", default = 0)
    parser.add_option("-c", "--cipher", dest = "cipher", help = "cipher specification for SSH (defaults to blowfish)", default = "blowfish")
    parser.add_option("-C", "--compress", dest = "compress", action = "store_true", help = "enable compression over SSH (defaults to on)", default = True)
    parser.add_option("-i", "--id", dest = "keyfile", help = "SSH public key file")
    parser.add_option("-P", "--pass", dest = "passenv", help = "environment variable containing SSH password (requires sshpass)")
    parser.add_option("-s", "--sudo", dest = "sudo", action = "store_true", help = "use sudo on the remote end (defaults to off)", default = False)
    parser.add_option("-n", "--dryrun", dest = "dryrun", action = "store_true", help = "do a dry run (don't write anything, just report differences)", default = False)
    parser.add_option("-T", "--createdest", dest = "createdest", action = "store_true", help = "create destination file using truncate(2)", default = False)
    parser.add_option("-S", "--script", dest = "script", help = "location of script on remote host (otherwise current script is sent over)")
    parser.add_option("-I", "--interpreter", dest = "interpreter", help = "[full path to] interpreter used to invoke remote server (defaults to python2)", default = "python2")
    parser.add_option("-t", "--interval", dest = "interval", type = "int", help = "interval between stats output (seconds, defaults to 1)", default = 1)
    (options, args) = parser.parse_args()

    if len(args) < 2:
        parser.print_help()
        print __doc__
        sys.exit(1)

    if args[0] == 'server':
        dstdev = args[1]
        server(dstdev, False, options)
    elif args[0] == 'tmpserver':
        dstdev = args[1]
        server(dstdev, True, options)
    else:
        srcdev = args[0]
        dsthost = args[1]
        if len(args) > 2:
            dstdev = args[2]
        else:
            dstdev = None

        if options.dryrun:
            print("Dryrun - will only report differences, no data will be written")
        else:
            print("\n!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            print("!!!                                          !!!")
            print("!!! DESTINATION WILL BE PERMANENTLY CHANGED! !!!")
            print("!!!         PRESS CTRL-C NOW TO EXIT         !!!")
            print("!!!                                          !!!")
            print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n")
            time.sleep(5)

        workers = {}
        for i in xrange(options.workers):
            pid = os.fork()
            if pid == 0:
                sync(i, srcdev, dsthost, dstdev, options)
                sys.exit(0)
            else:
                workers[pid] = i

        for i in xrange(options.workers):
            pid, err = os.wait()
            print "Worker #%d exited with %d" % (workers[pid], err)
