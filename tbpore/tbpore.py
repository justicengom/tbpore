import gzip
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Tuple

import click
import yaml
from loguru import logger

from tbpore import (
    TMP_NAME,
    H37RV_genome,
    H37RV_mask,
    __version__,
    cache_dir,
    config_file,
    external_scripts_dir,
)
from tbpore.cli import Mutex
from tbpore.external_tools import ExternalTool
from tbpore.utils import (
    concatenate_fastqs,
    find_fastq_files,
    parse_verbose_filter_params,
)

log_fmt = (
    "[<green>{time:YYYY-MM-DD HH:mm:ss}</green>] <level>{level: <8}</level> | "
    "<level>{message}</level>"
)


def load_config_file() -> Dict[Any, Any]:
    with open(config_file, "r") as config_file_fh:
        return yaml.safe_load(config_file_fh)


def concatenate_inputs_into_infile(
    inputs: Tuple[Path, ...], infile: Path, recursive: bool, ctx: click.Context
) -> None:
    if len(inputs) == 1 and inputs[0].is_file():
        if not inputs[0].suffix == ".gz":
            with open(inputs[0], "rb") as f_in:
                with gzip.open(infile, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
        else:
            shutil.copy2(inputs[0], infile)
    else:
        logger.info("Searching for fastq files...")
        fq_files = set()
        for p in inputs:
            if p.is_file():
                fq_files.add(p)
            else:
                fq_files.update(find_fastq_files(p, recursive))

        # remove non-trivial duplicates (e.g. when comparing Path/str objects and paths referring to the same file but
        # represented differently)
        fq_files = set(map(lambda file: Path(file).resolve(), fq_files))

        no_fastq_files_found = len(fq_files) == 0
        if no_fastq_files_found:
            logger.error(
                "No fastq files found for the given inputs, please check your input files/dirs."
            )
            logger.error(
                "Tip: to search for fastq files recursively, please use the --recursive flag."
            )
            ctx.exit(2)

        logger.info(f"Found {len(fq_files)} fastq files. Joining them...")
        concatenate_fastqs(fq_files, infile)


def setup_logging(verbose: bool, quiet: bool) -> None:
    log_lvl = "INFO"
    if verbose:
        log_lvl = "DEBUG"
    elif quiet:
        log_lvl = "ERROR"
    logger.remove()
    logger.add(sys.stderr, level=log_lvl, format=log_fmt)


@click.command()
@click.help_option("--help", "-h")
@click.version_option(__version__, "--version", "-V")
@click.option(
    "-o",
    "--outdir",
    help="Directory to place output files",
    default="tbpore_out",
    show_default=True,
    type=click.Path(file_okay=False, writable=True, path_type=Path),
)
@click.option(
    "-v",
    "--verbose",
    help="Turns on debug-level logger. ",
    is_flag=True,
    cls=Mutex,
    not_required_if=["quiet"],
)
@click.option(
    "-q",
    "--quiet",
    help="Turns off all logging except errors. ",
    is_flag=True,
    cls=Mutex,
    not_required_if=["verbose"],
)
@click.option(
    "-r", "--recursive", help="Recursively search INPUTS for fastq files", is_flag=True
)
@click.option(
    "--tmp",
    help=(
        f"Specify where to write all (tbpore) temporary files. [default: "
        f"<outdir>/{TMP_NAME}]"
    ),
    type=click.Path(file_okay=False, writable=True, path_type=Path),
)
@click.option(
    "-S",
    "--name",
    help=(
        "Name of the sample. By default, will use the first INPUT file with any "
        "extensions stripped"
    ),
)
@click.option(
    "-t",
    "--threads",
    help="Number of threads to use in multithreaded tools",
    type=int,
    show_default=True,
    default=1,
)
@click.option(
    "-A",
    "--report_all_mykrobe_calls",
    is_flag=True,
    default=False,
    show_default=True,
    help="Report all mykrobe calls (turn on flag -A, --report_all_calls when calling mykrobe)",
)
@click.option(
    "--cleanup/--no-cleanup",
    "-d/-D",
    default=False,
    show_default=True,
    help="Remove all temporary files on *successful* completion",
)
@click.argument("inputs", type=click.Path(exists=True, path_type=Path), nargs=-1)
@click.pass_context
def main(
    ctx: click.Context,
    verbose: bool,
    quiet: bool,
    outdir: Path,
    inputs: Tuple[Path, ...],
    recursive: bool,
    tmp: Path,
    name: str,
    threads: int,
    report_all_mykrobe_calls: bool,
    cleanup: bool,
):
    """Mycobacterium tuberculosis genomic analysis from Nanopore sequencing data

    INPUTS: Fastq file(s) and/or a directory containing fastq files. All files will
    be joined into a single fastq file, so ensure they're all part of the same
    sample/isolate.
    """
    setup_logging(verbose, quiet)
    logger.info(f"Welcome to TBpore version {__version__}")

    config = load_config_file()

    # create dirs for the run
    outdir.mkdir(exist_ok=True, parents=True)
    if tmp is None:
        tmp = outdir / TMP_NAME
    tmp.mkdir(exist_ok=True, parents=True)
    logdir = outdir / "logs"
    cache_dir.mkdir(parents=True, exist_ok=True)

    if not inputs:
        logger.error("No INPUT files given")
        ctx.exit(2)

    if not name:
        name = inputs[0].name.split(".")[0]
        if not name:
            name = "input"
        logger.debug(f"No sample name found; using '{name}'")

    infile = tmp / f"{name}.fq.gz"
    concatenate_inputs_into_infile(inputs, infile, recursive, ctx)

    report_all_mykrobe_calls_param = "-A" if report_all_mykrobe_calls else ""
    mykrobe_output = f"{outdir}/{name}.mykrobe.json"
    mykrobe = ExternalTool(
        tool="mykrobe",
        input=f"-i {infile}",
        output=f"-o {mykrobe_output}",
        params=f"predict {report_all_mykrobe_calls_param} --sample {name} -t {threads} --tmp {tmp} "
        f"--skeleton_dir {cache_dir} {config['mykrobe']['predict']['params']}",
        logdir=logdir,
    )

    subsampled_reads = f"{tmp}/{name}.subsampled.fastq.gz"
    rasusa = ExternalTool(
        tool="rasusa",
        input=f"-i {infile}",
        output=f"-o {subsampled_reads}",
        params=config["rasusa"]["params"],
        logdir=logdir,
    )

    sam_file = f"{tmp}/{name}.subsampled.sam"
    minimap = ExternalTool(
        tool="minimap2",
        input=f"{H37RV_genome} {subsampled_reads}",
        output=f"-o {sam_file}",
        params=f"-t {threads} {config['minimap2']['params']}",
        logdir=logdir,
    )

    sorted_sam_file = f"{tmp}/{name}.subsampled.sorted.sam"
    samtools_sort = ExternalTool(
        tool="samtools",
        input=sam_file,
        output=f"-o {sorted_sam_file}",
        params=f"sort -@ {threads} {config['samtools']['sort']['params']}",
        logdir=logdir,
    )

    pileup_file = f"{tmp}/{name}.subsampled.pileup.vcf"
    bcftools_mpileup = ExternalTool(
        tool="bcftools",
        input=sorted_sam_file,
        output=f"-o {pileup_file}",
        params=f"mpileup -f {H37RV_genome} --threads {threads} {config['bcftools']['mpileup']['params']}",
        logdir=logdir,
    )

    snps_file = f"{tmp}/{name}.subsampled.snps.vcf"
    bcftools_call = ExternalTool(
        tool="bcftools",
        input=pileup_file,
        output=f"-o {snps_file}",
        params=f"call --threads {threads} {config['bcftools']['call']['params']}",
        logdir=logdir,
    )

    filtered_snps_file = f"{outdir}/{name}.subsampled.snps.filtered.bcf"
    filtering_options = " ".join(
        (
            config["filter"]["params"],
            parse_verbose_filter_params(config["filter"]["verbose_params"]),
        )
    )
    filter_vcf = ExternalTool(
        tool=sys.executable,
        input=f"-i {snps_file}",
        output=f"-o {filtered_snps_file}",
        params=f"{external_scripts_dir/'apply_filters.py'} {filtering_options}",
        logdir=logdir,
    )

    consensus_file = f"{outdir}/{name}.consensus.fa"
    generate_consensus = ExternalTool(
        tool=sys.executable,
        input=f"-i {filtered_snps_file} -f {H37RV_genome} -m {H37RV_mask}",
        output=f"-o {consensus_file}",
        params=f"{external_scripts_dir/'consensus.py'} --sample-id {name} {config['consensus']['params']}",
        logdir=logdir,
    )

    tools_to_run = [
        mykrobe,
        rasusa,
        minimap,
        samtools_sort,
        bcftools_mpileup,
        bcftools_call,
        filter_vcf,
        generate_consensus,
    ]
    for tool in tools_to_run:
        try:
            tool.run()
        except subprocess.CalledProcessError as error:
            logger.error(
                f"Error calling {tool.command_as_str} (return code {error.returncode})"
            )
            logger.error(f"Please check stdout log file: {tool.out_log}")
            logger.error(f"Please check stderr log file: {tool.err_log}")
            logger.error("Temporary files are preserved for debugging")
            logger.error("Exiting...")
            ctx.exit(1)

    if cleanup:
        logger.info("Cleaning up temporary files...")
        shutil.rmtree(tmp)

    logger.success("Done")


if __name__ == "__main__":
    main()
