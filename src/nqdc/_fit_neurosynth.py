"""'fit_neurosynth' step.

Run a NeuroSynth-style Chi2 test of independence between term occurrence and
voxel activation (called "association test" on NeuroSynth website).
"""
import argparse
import logging
from pathlib import Path
import re
from typing import Optional, Tuple, Mapping

import numpy as np
import pandas as pd
import joblib
from scipy import stats, sparse

from nqdc import _model_fit_utils, _img_utils, _utils
from nqdc._typing import (
    PathLikeOrStr,
    BaseProcessingStep,
    ArgparseActions,
    NiftiMasker,
)

_LOG = logging.getLogger(__name__)
_STEP_NAME = "fit_neurosynth"
_STEP_DESCRIPTION = "Run a NeuroSynth meta-analysis on the downloaded data."
_STEP_HELP = (
    "Run a NeuroSynth-like meta-analysis on the downloaded "
    "data. This can be a more computationally intensive step for "
    "large datasets."
)


# Note: we don't use implementations from the neurosynth or nimare packages
# because (i) as of 2022-05-06 they use too much memory and (ii) to avoid
# adding a dependency. Users can easily run any nimare analysis on
# nqdc-generated data thanks to the 'extract_nimare_data' step.


def _chi_square(
    brain_maps: np.memmap,
    brain_maps_sum: np.ndarray,
    term_vector: sparse.csc_matrix,
) -> np.ndarray:
    """Test independence of `term_vector` and each voxel in `brain_maps`.

    Transforms the output to Z values and returns a vector of Z values of size
    n_voxels ie `brain_maps.shape[1]`.

    """
    assert term_vector.dtype == "int32"
    n_studies = brain_maps.shape[0]
    observed = np.empty((2, 2, brain_maps.shape[1]))
    term = term_vector.sum()
    noterm = n_studies - term
    vox = brain_maps_sum
    novox = n_studies - vox

    observed[1, 1, :] = term_vector.T.dot(brain_maps)
    observed[0, 1, :] = vox - observed[1, 1, :]
    observed[1, 0, :] = term - observed[1, 1, :]
    observed[0, 0, :] = noterm - observed[0, 1, :]

    expected = np.empty(observed.shape)
    expected[1, 1, :] = term * vox / n_studies
    expected[0, 1, :] = noterm * vox / n_studies
    expected[1, 0, :] = term * novox / n_studies
    expected[0, 0, :] = noterm * novox / n_studies
    expected_0 = expected == 0
    expected[expected_0] = 1

    cells = (observed - expected) ** 2 / expected
    cells[expected_0] = 0
    stat = np.sum(cells, axis=(0, 1))
    # degrees of freedom = (2 - 1) * (2 - 1) = 1
    z_values: np.ndarray = stats.norm().isf(stats.chi2(1).sf(stat) / 2)
    return z_values


def _term_to_file_name(term: str) -> str:
    return re.sub(r"\W", "_", term)


def _term_to_file_path(term: str, maps_dir: Path) -> Path:
    file_name = _term_to_file_name(term)
    return maps_dir.joinpath(f"{file_name}.nii.gz")


def _compute_meta_analysis_map(
    output_file: Path,
    brain_maps: np.memmap,
    brain_maps_sum: np.ndarray,
    masker: NiftiMasker,
    term_vector: sparse.csc_matrix,
) -> None:
    """Run chi2 test for every voxel; store resulting image in `output_dir`"""
    term_map = _chi_square(
        brain_maps,
        brain_maps_sum,
        term_vector,
    )
    img = masker.inverse_transform(term_map)
    img.to_filename(str(output_file))


class _NeuroSynthFit(_model_fit_utils.DataManager):
    """Helper class to load data and run the NeuroSynth analysis."""

    # storing in int32 is slightly faster (no conversion when computing sum or
    # dot product with tfidf vectors), but the difference is small and we
    # prefer to use less memory.
    _BRAIN_MAP_DTYPE = "int8"
    _VOXEL_SIZE = 2.0
    _TFIDF_THRESHOLD = 0.001

    def __init__(
        self,
        output_dir: Path,
        tfidf_dir: Path,
        extracted_data_dir: Path,
        n_jobs: int,
    ) -> None:
        super().__init__(
            tfidf_dir=tfidf_dir,
            extracted_data_dir=extracted_data_dir,
            n_jobs=n_jobs,
        )
        self.output_dir = output_dir
        self.output_dir.mkdir(exist_ok=True, parents=True)

    @staticmethod
    def _img_filter(
        coordinates: pd.DataFrame,
        masker: NiftiMasker,
        output: np.memmap,
        idx: int,
    ) -> None:
        _img_utils.ball_coords_to_masked_map(coordinates, masker, output, idx)

    def _write_output_data(self) -> None:
        """Save metadata and tfidf features."""
        assert self.feature_names is not None
        assert self.metadata is not None
        assert self.masker is not None

        self.feature_names["file_name"] = self.feature_names["term"].map(
            _term_to_file_name
        )
        self.feature_names.loc[:, ["term", "file_name"]].to_csv(
            str(self.output_dir.joinpath("terms.csv")),
            index=False,
        )
        self.metadata.to_csv(
            str(self.output_dir.joinpath("metadata.csv")), index=False
        )
        sparse.save_npz(str(self.output_dir.joinpath("tfidf.npz")), self.tfidf)
        self.masker.mask_img_.to_filename(
            str(self.output_dir.joinpath("brain_mask.nii.gz"))
        )

    def _fit_model(self) -> None:
        """Performs the actual analysis."""
        assert self.feature_names is not None
        assert self.tfidf is not None
        assert self.brain_maps is not None

        maps_dir = self.output_dir.joinpath("neurosynth_maps")
        maps_dir.mkdir(exist_ok=True)
        n_terms = len(self.feature_names)
        _LOG.info(f"Running NeuroSynth analysis for {n_terms} terms.")
        thresholded_tfidf = (
            self.tfidf.tocsc() > self._TFIDF_THRESHOLD
        ).astype("int32")
        maps_sum = self.brain_maps.sum(axis=0)
        joblib.Parallel(self.n_jobs, verbose=1)(
            joblib.delayed(_compute_meta_analysis_map)(
                _term_to_file_path(term, maps_dir),
                self.brain_maps,
                maps_sum,
                self.masker,
                term_tfidf.T,
            )
            for term, term_tfidf in zip(
                self.feature_names["term"].values, thresholded_tfidf.T
            )
        )
        self._write_output_data()


def fit_neurosynth(
    tfidf_dir: PathLikeOrStr,
    extracted_data_dir: Optional[PathLikeOrStr] = None,
    output_dir: Optional[PathLikeOrStr] = None,
    n_jobs: int = 1,
) -> Tuple[Path, int]:
    """Run a NeuroSyth-style meta-analysis

    (Chi2 test of independence between term occurrence and voxel activation).

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
        Directory in which to store the NeuroSynth maps. If not specified, a
        sibling directory of `vectorized_dir` whose name ends with
        `_neurosynthResults` is created. It will contain the images (of Z
        values) resulting from the analysis.

    Returns
    -------
    output_dir
        The directory in which the meta-analysis maps are stored.
    exit_code
        0 if the analysis ran successfully and 1 otherwise. Used by the
        `nqdc` command-line interface.
    """
    tfidf_dir = Path(tfidf_dir)
    extracted_data_dir = _utils.get_extracted_data_dir_from_tfidf_dir(
        tfidf_dir, extracted_data_dir
    )
    output_dir = _utils.get_output_dir(
        tfidf_dir, output_dir, "_vectorizedText", "_neurosynthResults"
    )
    status = _utils.check_steps_status(tfidf_dir, output_dir, __name__)
    if not status["need_run"]:
        return output_dir, 0
    _LOG.info(
        f"Performing NeuroSynth analysis with data from {tfidf_dir} "
        f"and {extracted_data_dir}."
    )
    _NeuroSynthFit(output_dir, tfidf_dir, extracted_data_dir, n_jobs).fit()
    _LOG.info(f"NeuroSynth results saved in {output_dir}.")
    is_complete = bool(status["previous_step_complete"])
    _utils.copy_static_files("_fit_neurosynth", output_dir)
    _utils.write_info(output_dir, name=_STEP_NAME, is_complete=is_complete)
    return output_dir, 0


class FitNeuroSynthStep(BaseProcessingStep):
    """Running NeuroSynth meta-analysis as part of a pipeline (nqdc run)."""

    name = _STEP_NAME
    short_description = _STEP_DESCRIPTION

    def edit_argument_parser(self, argument_parser: ArgparseActions) -> None:
        argument_parser.add_argument(
            "--fit_neurosynth", action="store_true", help=_STEP_HELP
        )
        _utils.add_n_jobs_argument(argument_parser)

    def run(
        self,
        args: argparse.Namespace,
        previous_steps_output: Mapping[str, Path],
    ) -> Tuple[Optional[Path], int]:
        if not args.fit_neurosynth:
            return None, 0
        return fit_neurosynth(
            previous_steps_output["vectorize"],
            previous_steps_output["extract_data"],
            n_jobs=args.n_jobs,
        )


class StandaloneFitNeuroSynthStep(BaseProcessingStep):
    """Running NeuroSynth as a standalone command (nqdc fit_neurosynth)."""

    name = _STEP_NAME
    short_description = _STEP_DESCRIPTION

    def edit_argument_parser(self, argument_parser: ArgparseActions) -> None:
        argument_parser.add_argument(
            "vectorized_data_dir",
            help="Directory containing TFIDF features and vocabulary. "
            "It is a directory created by nqdc whose name ends with "
            "'_vectorizedText'. A sibling directory will be created for "
            "the NeuroSynth results.",
        )
        _utils.add_n_jobs_argument(argument_parser)
        argument_parser.description = _STEP_HELP

    def run(
        self,
        args: argparse.Namespace,
        previous_steps_output: Mapping[str, Path],
    ) -> Tuple[Path, int]:
        return fit_neurosynth(args.vectorized_data_dir, n_jobs=args.n_jobs)
