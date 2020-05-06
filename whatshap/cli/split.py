"""
Split reads by haplotype.

Reads FASTQ/BAM file and a list of haplotype assignments (such as generated by
whatshap haplotag --output-haplotag-list). Outputs one FASTQ/BAM per haplotype.
BAM mode is intended for unmapped BAMs (such as provided by PacBio).
"""
import logging
import os
import pysam
from collections import defaultdict, Counter
import itertools
from argparse import SUPPRESS

from xopen import xopen

from contextlib import ExitStack
from whatshap.utils import detect_file_format
from whatshap.timer import StageTimer

logger = logging.getLogger(__name__)


# fmt: off
def add_arguments(parser):
    arg = parser.add_argument
    arg('--output-h1', default=None,
        help='Output file to write reads from Haplotype 1 to. Use ending .gz to '
        'create gzipped file.')
    arg('--output-h2', default=None,
        help='Output file to write reads from Haplotype 2 to. Use ending .gz to '
        'create gzipped file.')
    arg('--output-untagged', default=None,
        help='Output file to write untagged reads to. Use ending .gz to '
        'create gzipped file.')
    arg('--add-untagged', default=False, action='store_true',
        help='Add reads without tag to both H1 and H2 output streams.')
    arg('--pigz', dest='pigz_deprecated', action='store_true', help=SUPPRESS)
    arg('--only-largest-block', default=False, action='store_true',
        help='Only consider reads to be tagged if they belong to the largest '
        'phased block (in terms of read count) on their respective chromosome')
    arg('--discard-unknown-reads', default=False, action='store_true',
        help='Only check the haplotype of reads listed in the haplotag list file. '
            'Reads (read names) not contained in this file will be discarded. '
            'In the default case (= keep unknown reads), those reads would be '
            'considered untagged and end up in the respective output file. '
            'Please be sure that the read names match between the input FASTQ/BAM '
            'and the haplotag list file.')
    arg('--read-lengths-histogram', default=None,
        help='Output file to write read lengths histogram to in tab separated format.')
    arg('reads_file', metavar='READS', help='Input FASTQ/BAM file with reads (FASTQ can be gzipped)')
    arg('list_file', metavar='LIST',
        help='Tab-separated list with (at least) two columns <readname> and <haplotype> (can be gzipped). '
            'Currently, the two haplotypes have to be named H1 and H2 (or none). Alternatively, the '
            'output of the "haplotag" command can be used (4 columns), and this is required for using '
            'the "--only-largest-block" option (need phaseset and chromosome info).')
# fmt: on


def validate(args, parser):
    if (args.output_h1 is None) and (args.output_h2 is None) and (args.output_untagged is None):
        parser.error(
            "Nothing to be done since neither --output-h1 nor --output-h2 nor --output-untagged are given."
        )


def select_reads_in_largest_phased_blocks(block_sizes, block_to_readnames):
    """
    :param block_sizes:
    :param block_to_readnames:
    :return:
    """
    selected_reads = set()
    logger.info("Determining largest blocks/phasesets per chromosome")
    for chromosome, block_counts in block_sizes.items():
        block_name, reads_in_block = block_counts.most_common(1)[0]
        logger.info(
            "Chromosome: {} - Phaseset: {} - Tagged reads: {}".format(
                chromosome, block_name, reads_in_block
            )
        )
        selected_reads = selected_reads.union(set(block_to_readnames[(chromosome, block_name)]))
    logger.info(
        "Total number of haplo-tagged reads in all largest phased blocks: {}".format(
            len(selected_reads)
        )
    )
    return selected_reads


def process_haplotag_list_file(
    haplolist, line_parser, haplotype_to_int, only_largest_blocks, discard_unknown_reads
):
    """
    :param haplolist:
    :param line_parser:
    :param haplotype_to_int:
    :param only_largest_blocks:
    :param discard_unknown_reads:
    :return:
    """

    is_header = haplolist.readline().startswith("#")
    if not is_header:
        haplolist.seek(0)

    # needed to determine largest phased block
    block_sizes = defaultdict(Counter)
    # for later removal of reads not in largest phased block;
    # since this can grow quite a bit, only fill if needed
    blocks_to_readnames = defaultdict(set)

    # this set should not be too large given
    # that the haplotag list file contains only
    # a subset of the reads in the input FASTQ/BAM
    known_reads = set()

    readname_to_haplotype = defaultdict(int)
    total_reads = 0

    for line in haplolist:
        readname, haplo_name, phaseset, chromosome = line_parser(line)
        total_reads += 1
        try:
            haplo_num = haplotype_to_int[haplo_name]
        except KeyError:
            logger.error(
                "Mapping the haplotype name to the corresponding haplotype "
                "number failed. Currently, the haplotype name in the haplotag "
                "list file has to be one of: none, H1, H2. The value that triggered "
                "the error was: {}".format(haplo_name)
            )
            raise
        if haplo_num == 0:
            if discard_unknown_reads:
                known_reads.add(readname)
            # Some "trickery" here:
            # Haplotype 0 means untagged;
            # the return value of a defaultdict(int)
            # is zero for unknown keys, so no need to store
            # anything unless "--discard-unknown-reads" is True,
            # in which case we need to know all read names
            continue
        readname_to_haplotype[readname] = haplo_num
        if only_largest_blocks:
            block_sizes[chromosome][phaseset] += 1
            blocks_to_readnames[(chromosome, phaseset)].add(readname)

    tagged_reads = len(readname_to_haplotype)
    untagged_reads = total_reads - tagged_reads
    logger.info("Total number of reads in haplotag list: {}".format(total_reads))
    logger.info("Total number of haplo-tagged reads: {}".format(tagged_reads))
    logger.info("Total number of untagged reads: {}".format(untagged_reads))

    if discard_unknown_reads:
        known_reads = known_reads.union(set(readname_to_haplotype.keys()))
        num_known_reads = len(known_reads)
        assert (
            total_reads == num_known_reads
        ), "Mismatch between total number of reads and known reads: {} vs {}".format(
            total_reads, num_known_reads
        )

    if only_largest_blocks:
        selected_reads = select_reads_in_largest_phased_blocks(block_sizes, blocks_to_readnames)
        readname_to_haplotype = defaultdict(
            int, {k: readname_to_haplotype[k] for k in selected_reads}
        )
        num_removed_reads = total_reads - len(readname_to_haplotype)
        logger.info(
            "Number of reads removed / "
            "reads not overlapping largest phased blocks: {}".format(num_removed_reads)
        )

    return readname_to_haplotype, known_reads


def _two_column_parser(line):
    cols = line.strip().split("\t")[:2]
    return cols[0], cols[1], None, None


def _four_column_parser(line):
    return line.strip().split("\t")[:4]


def _bam_iterator(bam_file):
    """
    :param bam_file:
    :return:
    """
    for record in bam_file:
        qlen = record.query_length
        if qlen > 0:
            yield record.query_name, qlen, record
        else:
            inferred_qlen = record.infer_query_length()
            if inferred_qlen is not None:
                yield record.query_name, inferred_qlen, record
            else:
                yield record.query_name, 0, record


def _fastq_string_iterator(fastq_file):
    """
    Explicit casting to string because pysam does not seem to
    have a writer for FASTQ files - note that this relies
    on opening all compressed files in "text" mode

    :param fastq_file:
    :return:
    """
    for record in fastq_file:
        yield record.name, len(record.sequence), str(record) + "\n"


def _fastq_binary_iterator(fastq_file):
    """
    This one just exists for the pigz dependency
    :param fastq_file:
    :return:
    """
    for record in fastq_file:
        yield record.name, len(record.sequence), (str(record) + "\n").encode("utf-8")


def check_haplotag_list_information(haplotag_list, exit_stack):
    """
    Check if the haplotag list file has at least 4 columns
    (assumed to be read name, haplotype, phaseset, chromosome),
    or at least 2 columns (as above). Fails if the haplotag file
    is not tab-separated. Return suitable parser for format

    :param haplotag_list: Tab-separated file with at least 2 or 4 columns
    :param exit_stack:
    :return:
    """
    haplo_list = exit_stack.enter_context(xopen(haplotag_list))
    first_line = haplo_list.readline().strip()
    # rewind to make sure a header-less file is processed correctly
    haplo_list.seek(0)
    has_chrom_info = False
    try:
        _, _, _, _ = first_line.split("\t")[:4]
        line_parser = _four_column_parser
    except ValueError:
        try:
            _, _ = first_line.split("\t")[:2]
            line_parser = _two_column_parser
        except ValueError:
            raise ValueError(
                "First line of haplotag list file does not have "
                "at least 2 columns, or it is not tab-separated: {}".format(first_line)
            )
    else:
        has_chrom_info = True
    return haplo_list, has_chrom_info, line_parser


def initialize_io_files(reads_file, output_h1, output_h2, output_untagged, exit_stack):
    """
    :param reads_file:
    :param output_h1:
    :param output_h2:
    :param output_untagged:
    :param exit_stack:
    :return:
    """
    potential_fastq_extensions = [
        "fastq",
        "fastq.gz",
        "fastq.gzip" "fq",
        "fq.gz" "fq.gzip",
    ]
    input_format = detect_file_format(reads_file)
    if input_format is None:
        # TODO: this is a heuristic, need to extend utils::detect_file_format
        if any([reads_file.endswith(ext) for ext in potential_fastq_extensions]):
            input_format = "FASTQ"
        else:
            raise ValueError(
                "Undetected file format for input reads. "
                "Expecting BAM or FASTQ (gzipped): {}".format(reads_file)
            )
    elif input_format == "BAM":
        pass
    elif input_format in ["VCF", "CRAM"]:
        raise ValueError(
            "Input file format detected as: {} " "Currently, only BAM and FASTQ is supported."
        )
    else:
        # this means somebody changed utils::detect_file_format w/o
        # checking for usage throughout the code
        raise ValueError(
            "Unexpected file format for input reads: {} - "
            "Expecting BAM or FASTQ (gzipped)".format(input_format)
        )

    if input_format == "BAM":
        input_reader = exit_stack.enter_context(
            pysam.AlignmentFile(
                reads_file,
                mode="rb",
                check_sq=False,  # I guess this is needed for unaligned PacBio native files
            )
        )
        input_iter = _bam_iterator
        output_writers = dict()
        for hap, outfile in enumerate([output_untagged, output_h1, output_h2]):
            output_writers[hap] = exit_stack.enter_context(
                pysam.AlignmentFile(
                    os.devnull if outfile is None else outfile, mode="wb", template=input_reader,
                )
            )
    elif input_format == "FASTQ":
        # raw or gzipped is both handled by PySam
        input_reader = exit_stack.enter_context(pysam.FastxFile(reads_file))
        input_mode = "wb"
        if not (reads_file.endswith(".gz") or reads_file.endswith(".gzip")):
            input_mode = "w"
        input_iter = _fastq_string_iterator
        output_writers = dict()
        for hap, outfile in enumerate([output_untagged, output_h1, output_h2]):
            open_handle = exit_stack.enter_context(xopen(outfile, "w"))
            if open_handle is None:
                open_handle = exit_stack.enter_context(open(os.devnull, input_mode))
            output_writers[hap] = open_handle
    else:
        # and this means I overlooked something...
        raise ValueError("Unhandled file format for input reads: {}".format(input_format))
    return input_reader, input_iter, output_writers


def write_read_length_histogram(length_counts, path):
    h1 = length_counts[1]
    h2 = length_counts[2]
    untag = length_counts[0]
    all_read_lengths = sorted(itertools.chain(*(h1.keys(), h2.keys(), untag.keys())))
    with xopen(path, "w") as tsv_file:
        print("#length", "count-untagged", "count-h1", "count-h2", sep="\t", file=tsv_file)
        for rlen in all_read_lengths:
            print(rlen, untag[rlen], h1[rlen], h2[rlen], sep="\t", file=tsv_file)


def run_split(
    reads_file,
    list_file,
    output_h1=None,
    output_h2=None,
    output_untagged=None,
    add_untagged=False,
    pigz_deprecated=False,
    only_largest_block=False,
    discard_unknown_reads=False,
    read_lengths_histogram=None,
):
    if pigz_deprecated:
        logger.warning("Ignoring deprecated --pigz option")
    timers = StageTimer()
    timers.start("split-run")

    with ExitStack() as stack:
        timers.start("split-init")

        # TODO: obviously this won't work for more than two haplotypes
        haplotype_to_int = {"none": 0, "H1": 1, "H2": 2}

        haplo_list, has_haplo_chrom_info, line_parser = check_haplotag_list_information(
            list_file, stack
        )

        if only_largest_block:
            logger.debug(
                'User selected "--only-largest-block", this requires chromosome '
                "and phaseset information to be present in the haplotag list file."
            )
            if not has_haplo_chrom_info:
                raise ValueError(
                    "The haplotag list file does not contain phaseset and chromosome "
                    "information, which is required to select only reads from the "
                    "largest phased block. Columns 3 and 4 are missing."
                )

        timers.start("split-process-haplotag-list")

        readname_to_haplotype, known_reads = process_haplotag_list_file(
            haplo_list, line_parser, haplotype_to_int, only_largest_block, discard_unknown_reads,
        )
        if discard_unknown_reads:
            logger.debug(
                "User selected to discard unknown reads, i.e., ignore all reads "
                "that are not part of the haplotag list input file."
            )
            assert (
                len(known_reads) > 0
            ), "No known reads in input set - would discard everything, this is probably wrong"
            missing_reads = len(known_reads)
        else:
            missing_reads = -1

        timers.stop("split-process-haplotag-list")

        input_reader, input_iterator, output_writers = initialize_io_files(
            reads_file, output_h1, output_h2, output_untagged, stack,
        )

        timers.stop("split-init")

        histogram_data = {
            0: Counter(),
            1: Counter(),
            2: Counter(),
        }

        # holds count statistics about total processed reads etc.
        read_counter = Counter()

        process_haplotype = {
            0: output_untagged is not None or add_untagged,
            1: output_h1 is not None,
            2: output_h2 is not None,
        }

        timers.start("split-iter-input")

        for read_name, read_length, record in input_iterator(input_reader):
            read_counter["total_reads"] += 1
            if discard_unknown_reads and read_name not in known_reads:
                read_counter["unknown_reads"] += 1
                continue
            read_haplotype = readname_to_haplotype[read_name]
            if not process_haplotype[read_haplotype]:
                read_counter["skipped_reads"] += 1
                continue
            histogram_data[read_haplotype][read_length] += 1
            read_counter[read_haplotype] += 1

            output_writers[read_haplotype].write(record)
            if read_haplotype == 0 and add_untagged:
                output_writers[1].write(record)
                output_writers[2].write(record)

            if discard_unknown_reads:
                missing_reads -= 1
                if missing_reads == 0:
                    logger.info("All known reads processed - cancel processing...")
                    break

        timers.stop("split-iter-input")

        if read_lengths_histogram is not None:
            timers.start("split-length-histogram")
            write_read_length_histogram(histogram_data, read_lengths_histogram)
            timers.stop("split-length-histogram")

    timers.stop("split-run")

    logger.info("\n== SUMMARY ==")
    logger.info("Total reads processed: {}".format(read_counter["total_reads"]))
    logger.info('Number of output reads "untagged": {}'.format(read_counter[0]))
    logger.info("Number of output reads haplotype 1: {}".format(read_counter[1]))
    logger.info("Number of output reads haplotype 2: {}".format(read_counter[2]))
    logger.info("Number of unknown (dropped) reads: {}".format(read_counter["unknown_reads"]))
    logger.info(
        "Number of skipped reads (per user request): {}".format(read_counter["skipped_reads"])
    )

    logger.info(
        "Time for processing haplotag list: {} sec".format(
            round(timers.elapsed("split-process-haplotag-list"), 3)
        )
    )

    logger.info(
        "Time for total initial setup: {} sec".format(round(timers.elapsed("split-init"), 3))
    )

    logger.info(
        "Time for iterating input reads: {} sec".format(
            round(timers.elapsed("split-iter-input"), 3)
        )
    )

    if read_lengths_histogram is not None:
        logger.info(
            "Time for creating histogram output: {} sec".format(
                round(timers.elapsed("split-length-histogram"), 3)
            )
        )

    logger.info("Total run time: {} sec".format(round(timers.elapsed("split-run"), 3)))


def main(args):
    run_split(**vars(args))
