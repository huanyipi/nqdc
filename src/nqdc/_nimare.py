"""'extract_nimare_data' step: build a NiMARE json dataset.

https://nimare.readthedocs.io/
"""
from pathlib import Path
import logging
import tempfile
import re
import argparse
from typing import Dict, Any, Mapping, Tuple, Optional

import numpy as np
from scipy import sparse
import pandas as pd

try:
    import nimare
    import nimare.io
except ImportError:
    _NIMARE_INSTALLED = False
else:
    _NIMARE_INSTALLED = True

from nqdc._typing import PathLikeOrStr, BaseProcessingStep, ArgparseActions
from nqdc import _utils

_LOG = logging.getLogger(__name__)
_STEP_NAME = "extract_nimare_data"
_STEP_DESCRIPTION = "Create a NiMARE JSON dataset from extracted data."


def _get_vocabulary_name(vectorized_dir: Path) -> str:
    match = re.match(r".*-voc_([^-_]+)_vectorizedText", vectorized_dir.name)
    if match is None:
        return "UNKNOWN"
    return match.group(1)


def _get_nimare_dataset_name(vectorized_dir: Path) -> str:
    return re.sub(
        r"^(.*?)(_vectorizedText)?$",
        r"\1_nimareDataset",
        vectorized_dir.name,
    )


def _collapse_authors(authors: pd.DataFrame) -> pd.Series:
    """Collapse author info into one entry per article.

    nqdc authors have one row per author per article, neurosynth and nimare use
    a single string for all authors in an article.
    """
    collapsed_authors, author_pmcids = [], []
    for pmcid, article_authors in authors.groupby("pmcid"):
        collapsed_authors.append(
            "; ".join(
                article_authors["surname"]
                .str.cat(article_authors["given-names"], sep=", ", na_rep="")
                .values
            )
        )
        author_pmcids.append(pmcid)
    return pd.Series(collapsed_authors, index=author_pmcids, name="authors")


def _collect_nimare_data(
    extracted_data_dir: Path, vectorized_dir: Path
) -> Dict[str, Any]:
    """Extract data needed for a NiMARE dataset from nqdc data dir."""
    metadata = pd.read_csv(extracted_data_dir.joinpath("metadata.csv"))
    metadata.rename(
        columns={"pmcid": "id", "publication_year": "year"}, inplace=True
    )
    space = pd.read_csv(extracted_data_dir.joinpath("coordinate_space.csv"))
    # false positive: pylint thinks read_csv returns a TextFileReader
    # pylint: disable-next=unsupported-assignment-operation
    metadata["space"] = space["coordinate_space"]
    authors = pd.read_csv(extracted_data_dir.joinpath("authors.csv"))
    collapsed_authors = _collapse_authors(authors)
    # false positive: pylint thinks read_csv returns a TextFileReader
    # pylint: disable-next=no-member
    metadata = metadata.join(collapsed_authors, on="id")
    coordinates = pd.read_csv(extracted_data_dir.joinpath("coordinates.csv"))
    coordinates.rename(columns={"pmcid": "id"}, inplace=True)
    vocabulary = pd.read_csv(
        vectorized_dir.joinpath("feature_names.csv"), header=None
    )
    tfidf = sparse.load_npz(vectorized_dir.joinpath("merged_tfidf.npz"))
    pmcids = np.loadtxt(str(vectorized_dir.joinpath("pmcid.txt")), dtype=int)
    metadata = metadata.set_index("id").loc[pmcids].reset_index()
    vocabulary_name = _get_vocabulary_name(vectorized_dir)
    return {
        "vocabulary": vocabulary,
        "vocabulary_name": vocabulary_name,
        "tfidf": tfidf,
        "metadata": metadata,
        "coordinates": coordinates,
    }


def _write_nimare_data(
    nimare_data: Mapping[str, Any], tmp_dir: Path
) -> Dict[str, Any]:
    """Write data to a temp dir in the layout NiMARE expects."""
    target_metadata = tmp_dir.joinpath("metadata.tsv.gz")
    nimare_data["metadata"].to_csv(target_metadata, sep="\t", index=False)
    target_coordinates = tmp_dir.joinpath("coordinates.tsv.gz")
    nimare_data["coordinates"].to_csv(
        target_coordinates, sep="\t", index=False
    )
    target_vocabulary = tmp_dir.joinpath("vocabulary.txt.gz")
    nimare_data["vocabulary"].iloc[:, 0].to_csv(
        target_vocabulary,
        sep="\t",
        header=None,
        index=False,
    )
    target_features = tmp_dir.joinpath(
        f"vocab-{nimare_data['vocabulary_name']}_source-combined_"
        "type-tfidf_features.npz"
    )
    sparse.save_npz(
        str(target_features),
        nimare_data["tfidf"],
    )
    return {
        "coordinates": str(target_coordinates),
        "metadata": str(target_metadata),
        "annotation_files": [
            {
                "vocabulary": str(target_vocabulary),
                "features": str(target_features),
            }
        ],
    }


def make_nimare_dataset(
    vectorized_dir: PathLikeOrStr,
    extracted_data_dir: Optional[PathLikeOrStr] = None,
    output_dir: Optional[PathLikeOrStr] = None,
) -> Tuple[Optional[Path], int]:
    """Create a NiMARE JSON dataset from data collected by `nqdc`.

    See the [NiMARE documentation](https://nimare.readthedocs.io/) for details.
    This function requires `nimare` to be installed.

    Parameters
    ----------
    vectorized_dir
        The directory containing the vectorized text (TFIDF features). It is
        the directory created by `nqdc.vectorize_corpus_to_npz` using
        `extracted_data_dir` as input.
    extracted_data_dir
        The directory containing extracted metadata and coordinates. It is a
        directory created by `nqdc.extract_data_to_csv`. If `None`, this
        function looks for a sibling directory of the `vectorized_dir` whose
        name ends with `_extractedData`.
    output_dir
        Directory in which to store the extracted data. If not specified, a
        sibling directory of `vectorized_dir` whose name ends with
        `_nimareDataset` is created.

    Returns
    -------
    output_dir
        The directory in which the NiMARE dataset is stored. It contains a
        `nimare_dataset.json` file.
    exit_code
        0 if the NiMARE was created and 1 otherwise. Used by the `nqdc`
        command-line interface.

    """
    vectorized_dir = Path(vectorized_dir)
    extracted_data_dir = _utils.get_extracted_data_dir_from_tfidf_dir(
        vectorized_dir, extracted_data_dir
    )
    if output_dir is None:
        output_dir = vectorized_dir.with_name(
            _get_nimare_dataset_name(vectorized_dir)
        )
    else:
        output_dir = Path(output_dir)
    status = _utils.check_steps_status(vectorized_dir, output_dir, __name__)
    if not status["need_run"]:
        return output_dir, 0
    if not _NIMARE_INSTALLED:
        _LOG.error(
            "NiMARE is not installed. Skipping creation of NiMARE dataset."
        )
        return None, 1
    _LOG.info(f"Beginning creation of NiMARE dataset in {output_dir}")
    nimare_data = _collect_nimare_data(extracted_data_dir, vectorized_dir)
    with tempfile.TemporaryDirectory() as tmp_dir:
        nimare_params = _write_nimare_data(nimare_data, Path(tmp_dir))
        output_dir.mkdir(exist_ok=True, parents=True)
        nimare.io.convert_neurosynth_to_json(
            nimare_params["coordinates"],
            nimare_params["metadata"],
            str(output_dir.joinpath("nimare_dataset.json")),
            annotations_files=nimare_params["annotation_files"],
        )
    is_complete = bool(status["previous_step_complete"])
    _utils.write_info(output_dir, name=_STEP_NAME, is_complete=is_complete)
    _LOG.info(f"Done creating NiMARE dataset in {output_dir}")
    return output_dir, 0


class NimareStep(BaseProcessingStep):
    """nimare as part of a pipeline (nqdc run)."""

    name = _STEP_NAME
    short_description = _STEP_DESCRIPTION

    def edit_argument_parser(self, argument_parser: ArgparseActions) -> None:
        argument_parser.add_argument(
            "--nimare",
            action="store_true",
            help="Create a NiMARE JSON dataset from extracted data. See the "
            "NiMARE documentation for details: https://nimare.readthedocs.io/"
            "en/latest/generated/nimare.dataset.Dataset.html#nimare."
            "dataset.Dataset . This option requires nimare to be installed.",
        )

    def run(
        self,
        args: argparse.Namespace,
        previous_steps_output: Mapping[str, Path],
    ) -> Tuple[Optional[Path], int]:
        if not args.nimare:
            return None, 0
        return make_nimare_dataset(
            previous_steps_output["vectorize"],
            previous_steps_output["extract_data"],
        )


class StandaloneNimareStep(BaseProcessingStep):
    """nimare as a standalone command (nqdc extract_nimare_data)."""

    name = _STEP_NAME
    short_description = _STEP_DESCRIPTION

    def edit_argument_parser(self, argument_parser: ArgparseActions) -> None:
        argument_parser.add_argument(
            "vectorized_data_dir",
            help="Directory containing TFIDF features and vocabulary. "
            "It is a directory created by nqdc whose name ends with "
            "'_vectorizedText'. A sibling directory will be created for "
            "the NiMARE dataset.",
        )
        argument_parser.description = self.short_description

    def run(
        self,
        args: argparse.Namespace,
        previous_steps_output: Mapping[str, Path],
    ) -> Tuple[Optional[Path], int]:
        return make_nimare_dataset(args.vectorized_data_dir)
