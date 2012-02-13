#!/usr/bin/env python
# -*- coding: UTF-8 -*-

import sys
import logging
import collections

import numpy as np
from optparse import OptionParser

from jcvi.formats.bed import Bed, BedLine
from jcvi.formats.blast import BlastLine
from jcvi.formats.base import BaseFile, read_block
from jcvi.utils.grouper import Grouper
from jcvi.apps.base import ActionDispatcher, debug
debug()


class AnchorFile (BaseFile):

    def __init__(self, filename):
        super(AnchorFile, self).__init__(filename)

    def iter_blocks(self, minsize=0):
        fp = open(self.filename)
        for header, lines in read_block(fp, "#"):
            lines = [x.split() for x in lines]
            if len(lines) >= minsize:
                yield zip(*lines)


def _score(cluster):
    """
    score of the cluster, in this case, is the number of non-repetitive matches
    """
    x, y = zip(*cluster)
    return min(len(set(x)), len(set(y)))


def group_hits(blasts):
    # grouping the hits based on chromosome pair
    all_hits = collections.defaultdict(list)
    for b in blasts:
        all_hits[(b.qseqid, b.sseqid)].append((b.qi, b.si))

    return all_hits


def read_blast(blast_file, qorder, sorder, is_self=False):
    """
    read the blast and convert name into coordinates
    """
    fp = open(blast_file)
    filtered_blast = []
    seen = set()
    for row in fp:
        b = BlastLine(row)
        query, subject = b.query, b.subject
        if query not in qorder or subject not in sorder:
            continue

        key = query, subject
        if key in seen:
            continue
        seen.add(key)

        qi, q = qorder[query]
        si, s = sorder[subject]

        if is_self and qi > si:
            # remove redundant a<->b to one side when doing self-self BLAST
            query, subject = subject, query
            qi, si = si, qi
            q, s = s, q

        b.qseqid, b.sseqid = q.seqid, s.seqid
        b.qi, b.si = qi, si

        filtered_blast.append(b)

    return filtered_blast


def read_anchors(anchor_file, qorder, sorder):
    """
    anchors file are just (geneA, geneB) pairs (with possible deflines)
    """
    all_anchors = collections.defaultdict(list)
    fp = open(anchor_file)
    for row in fp:
        if row[0] == '#':
            continue
        a, b = row.split()
        if a not in qorder or b not in sorder:
            continue
        qi, q = qorder[a]
        si, s = sorder[b]
        all_anchors[(q.seqid, s.seqid)].append((qi, si))

    return all_anchors


def synteny_scan(points, xdist, ydist, N):
    """
    This is the core single linkage algorithm which behaves in O(n):
    iterate through the pairs, foreach pair we look back on the
    adjacent pairs to find links
    """
    clusters = Grouper()
    n = len(points)
    points.sort()
    for i in xrange(n):
        for j in xrange(i - 1, -1, -1):
            # x-axis distance
            del_x = points[i][0] - points[j][0]
            if del_x > xdist:
                break
            # y-axis distance
            del_y = points[i][1] - points[j][1]
            if abs(del_y) > ydist:
                continue
            # otherwise join
            clusters.join(points[i], points[j])

    # select clusters that are at least >=N
    clusters = [sorted(cluster) for cluster in list(clusters) \
            if _score(cluster) >= N]

    return clusters


def batch_scan(points, xdist=20, ydist=20, N=6):
    """
    runs synteny_scan() per chromosome pair
    """
    chr_pair_points = group_hits(points)

    clusters = []
    for chr_pair in sorted(chr_pair_points.keys()):
        points = chr_pair_points[chr_pair]
        #logging.debug("%s: %d" % (chr_pair, len(points)))
        clusters.extend(synteny_scan(points, xdist, ydist, N))

    return clusters


def synteny_liftover(points, anchors, dist):
    """
    This is to get the nearest anchors for all the points (useful for the
    `liftover` operation below).
    """
    from scipy.spatial import cKDTree

    points = np.array(points)
    ppoints = points[:, :2] if points.shape[1] > 2 else points
    tree = cKDTree(anchors, leafsize=16)
    #print tree.data
    dists, idxs = tree.query(ppoints, p=1, distance_upper_bound=dist)
    #print [(d, idx) for (d, idx) in zip(dists, idxs) if idx!=tree.n]

    for point, dist, idx in zip(points, dists, idxs):
        # nearest is out of range
        if idx == tree.n:
            continue
        yield point


def add_beds(p):

    p.add_option("--qbed", help="Path to qbed (required)")
    p.add_option("--sbed", help="Path to sbed (required)")


def check_beds(p, opts):

    if not (opts.qbed and opts.sbed):
        print >> sys.stderr, "Options --qbed and --sbed are required"
        sys.exit(not p.print_help())

    qbed_file, sbed_file = opts.qbed, opts.sbed
    # is this a self-self blast?
    is_self = (qbed_file == sbed_file)
    if is_self:
        logging.debug("Looks like self-self comparison.")

    qbed = Bed(opts.qbed)
    sbed = Bed(opts.sbed)
    qorder = qbed.order
    sorder = sbed.order

    return qbed, sbed, qorder, sorder, is_self


def add_options(p, args):
    """
    scan and liftover has similar interfaces, so share common options
    returns opts, files
    """
    add_beds(p)
    p.add_option("--dist", default=10, type="int",
            help="Extent of flanking regions to search [default: %default]")

    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    blast_file, anchor_file = args

    return blast_file, anchor_file, opts.dist, opts


def main():

    actions = (
        ('scan', 'get anchor list using single-linkage algorithm'),
        ('depth', 'calculate the depths in the two genomes in comparison'),
        ('liftover', 'given anchor list, pull adjancent pairs from blast file'),
        ('breakpoint', 'identify breakpoints where collinearity ends'),
        ('cluster', 'cluster the segments and form PAD'),
            )

    p = ActionDispatcher(actions)
    p.dispatch(globals())


def get_segments(ranges, extra, minsegment=40):
    """
    Given a list of Range, perform chaining on the ranges and select a highest
    scoring subset and cut based on their boundaries. Let's say the projection
    of the synteny blocks onto one axis look like the following.

    1=====10......20====30....35====~~

    Then the segmentation will yield a block [1, 20), [20, 35), using an
    arbitrary right extension rule. Extra are additional end breaks for
    chromosomes.
    """
    from jcvi.utils.iter import pairwise
    from jcvi.utils.range import range_chain, LEFT, RIGHT

    NUL = -1
    selected, score = range_chain(ranges)

    endpoints = [(x.start, NUL) for x in selected]
    endpoints += [(x[0], LEFT) for x in extra]
    endpoints += [(x[1], RIGHT) for x in extra]
    endpoints.sort()

    current_left = 0
    for a, ai in endpoints:

        if ai == LEFT:
            current_left = a
        if ai == RIGHT:
            yield current_left, a
        elif ai == NUL:
            if a - current_left < minsegment:
                continue
            yield current_left, a - 1
            current_left = a


def write_PAD_bed(bedfile, prefix, pads, bed):
    fw = open(bedfile, "w")
    padnames = ["{0}:{1:05d}-{2:05d}".format(prefix, a, b) for a, b in pads]
    j = 0

    # Assign all genes to new partitions
    for i, x in enumerate(bed):
        a, b = pads[j]
        if i > b:
            j += 1
            a, b = pads[j]
        print >> fw, "\t".join((padnames[j], str(i), str(i + 1), x.accn))

    fw.close()

    npads = len(pads)
    logging.debug("{0} partition written in `{1}`.".format(npads, bedfile))
    return npads, padnames


def cluster(args):
    """
    %prog cluster blastfile anchorfile --qbed qbedfile --sbed sbedfile

    Cluster the segments and form PAD. This is the method described in Tang et
    al. (2010) PNAS paper. The anchorfile defines a list of synteny blocks,
    based on which the genome on one or both axis can be chopped up into pieces
    and clustered.
    """
    import numpy as np

    from math import log
    from jcvi.utils.range import Range

    p = OptionParser(cluster.__doc__)
    add_beds(p)
    p.add_option("--minsize", default=10, type="int",
                 help="Only segment using blocks >= size [default: %default]")
    p.add_option("--path", default="~/scratch/bin",
                 help="Path to the CLUSTER 3.0 binary [default: %default]")

    opts, args = p.parse_args(args)
    qbed, sbed, qorder, sorder, is_self = check_beds(p, opts)

    if len(args) != 2:
        sys.exit(not p.print_help())

    blastfile, anchorfile = args
    minsize = opts.minsize
    ac = AnchorFile(anchorfile)
    qranges, sranges = [], []
    qextra = [x[1:] for x in qbed.get_breaks()]
    sextra = [x[1:] for x in sbed.get_breaks()]

    id = 0
    for q, s in ac.iter_blocks(minsize=minsize):
        q = [qorder[x][0] for x in q]
        s = [sorder[x][0] for x in s]
        minq, maxq = min(q), max(q)
        mins, maxs = min(s), max(s)
        id += 1

        qr = Range("0", minq, maxq, maxq - minq, id)
        sr = Range("0", mins, maxs, maxs - mins, id)
        qranges.append(qr)
        sranges.append(sr)

    qpads = list(get_segments(qranges, qextra))
    spads = list(get_segments(sranges, sextra))

    suffix = ".pad.bed"
    qpf = opts.qbed.split(".")[0]
    spf = opts.sbed.split(".")[0]
    qpadfile = qpf + suffix
    spadfile = spf + suffix
    qnpads, qpadnames = write_PAD_bed(qpadfile, qpf, qpads, qbed)
    snpads, spadnames = write_PAD_bed(spadfile, spf, spads, sbed)
    m, n = qnpads, snpads

    qpadbed, spadbed = Bed(qpadfile), Bed(spadfile)
    qpadorder, spadorder = qpadbed.order, spadbed.order
    qpadid = dict((a, i) for i, a in enumerate(qpadnames))
    spadid = dict((a, i) for i, a in enumerate(spadnames))
    qpadlen = dict((a, len(b)) for a, b in qpadbed.sub_beds())
    spadlen = dict((a, len(b)) for a, b in spadbed.sub_beds())

    # Populate arrays of observed counts and expected counts
    logging.debug("Initialize array of size ({0} x {1})".format(m, n))
    observed = np.zeros((m, n))
    fp = open(blastfile)
    all_dots = 0
    for row in fp:
        b = BlastLine(row)
        qi, q = qpadorder[b.query]
        si, s = spadorder[b.subject]
        qseqid, sseqid = q.seqid, s.seqid
        qsi, ssi = qpadid[qseqid], spadid[sseqid]
        observed[qsi, ssi] += 1
        all_dots += 1

    assert int(round(observed.sum())) == all_dots

    S = len(qbed) * len(sbed)
    expected = np.zeros((m, n))
    for i, a in enumerate(qpadnames):
        alen = qpadlen[a]
        for j, b in enumerate(spadnames):
            blen = spadlen[b]
            expected[i, j] = all_dots * alen * blen * 1. / S

    assert int(round(expected.sum())) == all_dots

    # Calculate the statistical significance for each cell
    from scipy.stats.distributions import poisson
    M = m * n  # multiple testing
    logmp = np.zeros((m, n))
    for i in xrange(m):
        for j in xrange(n):
            obs, exp = observed[i, j], expected[i, j]
            logmp[i, j] = max(- log(M * poisson.pmf(obs, exp)), 0)

    matrixfile = ".".join((qpf, spf, "logmp.txt"))
    fw = open(matrixfile, "w")
    header = ["o"] + spadnames
    print >> fw, "\t".join(header)
    for i in xrange(m):
        row = [qpadnames[i]] + ["{0:.1f}".format(x) for x in logmp[i, :]]
        print >> fw, "\t".join(row)

    fw.close()


def depth(args):
    """
    %prog depth anchorfile --qbed qbedfile --sbed sbedfile

    Calculate the depths in the two genomes in comparison, given in --qbed and
    --sbed. The synteny blocks will be layered on the genomes, and the
    multiplicity will be summarized to stderr.
    """
    from jcvi.utils.range import range_depth

    p = OptionParser(depth.__doc__)
    add_beds(p)

    opts, args = p.parse_args(args)
    qbed, sbed, qorder, sorder, is_self = check_beds(p, opts)

    if len(args) != 1:
        sys.exit(not p.print_help())

    anchorfile, = args
    ac = AnchorFile(anchorfile)
    qranges = []
    sranges = []
    for q, s in ac.iter_blocks():
        q = [qorder[x] for x in q]
        s = [sorder[x] for x in s]
        qrange = (min(q)[0], max(q)[0])
        srange = (min(s)[0], max(s)[0])
        qranges.append(qrange)
        sranges.append(srange)

    qgenome = qbed.filename.split(".")[0]
    sgenome = sbed.filename.split(".")[0]
    print >> sys.stderr, "Genome {0} depths:".format(qgenome)
    range_depth(qranges, len(qbed))
    print >> sys.stderr, "Genome {0} depths:".format(sgenome)
    range_depth(sranges, len(sbed))


def get_blocks(scaffold, bs, order, xdist=20, ydist=20, N=6):
    points = []
    for b in bs:
        accn = b.accn.rsplit(".", 1)[0]
        if accn not in order:
            continue
        x, xx = order[accn]
        y = (b.start + b.end) / 2
        points.append((x, y))

    #print scaffold, points
    blocks = synteny_scan(points, xdist, ydist, N)
    return blocks


def breakpoint(args):
    """
    %prog breakpoint blastfile bedfile

    Identify breakpoints where collinearity ends. `blastfile` contains mapping
    from markers (query) to scaffolds (subject). `bedfile` contains marker
    locations in the related species.
    """
    from jcvi.formats.blast import bed
    from jcvi.utils.range import range_interleave

    p = OptionParser(breakpoint.__doc__)
    p.add_option("--xdist", type="int", default=20,
                 help="xdist (in related genome) cutoff [default: %default]")
    p.add_option("--ydist", type="int", default=200000,
                 help="ydist (in current genome) cutoff [default: %default]")
    p.add_option("-n", type="int", default=5,
                 help="number of markers in a block [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    blastfile, bedfile = args
    order = Bed(bedfile).order
    blastbedfile = bed([blastfile])
    bbed = Bed(blastbedfile)
    key = lambda x: x[1]
    for scaffold, bs in bbed.sub_beds():
        blocks = get_blocks(scaffold, bs, order,
                            xdist=opts.xdist, ydist=opts.ydist, N=opts.n)
        sblocks = []
        for block in blocks:
            xx, yy = zip(*block)
            sblocks.append((scaffold, min(yy), max(yy)))
        iblocks = range_interleave(sblocks)
        for ib in iblocks:
            ch, start, end = ib
            print "{0}\t{1}\t{2}".format(ch, start - 1, end)


def scan(args):
    """
    %prog scan blastfile anchor_file [options]

    pull out syntenic anchors from blastfile based on single-linkage algorithm
    """
    from jcvi.utils.cbook import SummaryStats

    p = OptionParser(scan.__doc__)
    p.add_option("-n", type="int", default=5,
            help="minimum number of anchors in a cluster [default: %default]")

    blast_file, anchor_file, dist, opts = add_options(p, args)
    qbed, sbed, qorder, sorder, is_self = check_beds(p, opts)

    filtered_blast = read_blast(blast_file, qorder, sorder, is_self=is_self)

    fw = open(anchor_file, "w")
    clusters = batch_scan(filtered_blast, xdist=dist, ydist=dist, N=opts.n)
    for cluster in clusters:
        print >>fw, "###"
        for qi, si in cluster:
            query, subject = qbed[qi].accn, sbed[si].accn
            print >>fw, "\t".join((query, subject))

    nclusters = len(clusters)
    nanchors = [len(c) for c in clusters]
    print >> sys.stderr, "A total of {0} anchors found in {1} clusters.".\
                  format(sum(nanchors), nclusters)
    print >> sys.stderr, SummaryStats(nanchors)


def liftover(args):
    """
    %prog liftover blastfile anchorfile [options]

    Typical use for this program is given a list of anchors (syntennic
    genes), choose from the blastfile the pairs that are close to the anchors.

    Anchorfile has the following format, each row defines a pair.

        geneA geneB
        geneC geneD
    """
    p = OptionParser(liftover.__doc__)

    blast_file, anchor_file, dist, opts = add_options(p, args)
    qbed, sbed, qorder, sorder, is_self = check_beds(p, opts)

    filtered_blast = read_blast(blast_file, qorder, sorder, is_self=is_self)
    all_hits = group_hits(filtered_blast)
    all_anchors = read_anchors(anchor_file, qorder, sorder)

    # select hits that are close to the anchor list
    j = 0
    fw = sys.stdout
    for chr_pair in sorted(all_anchors.keys()):
        hits = np.array(all_hits[chr_pair])
        anchors = np.array(all_anchors[chr_pair])

        #logging.debug("%s: %d" % (chr_pair, len(anchors)))
        if not len(hits):
            continue

        for point in synteny_liftover(hits, anchors, dist):
            qi, si = point[:2]
            query, subject = qbed[qi].accn, sbed[si].accn
            print >>fw, "\t".join((query, subject, "lifted"))
            j += 1

    logging.debug("%d new pairs found" % j)


if __name__ == '__main__':
    main()
