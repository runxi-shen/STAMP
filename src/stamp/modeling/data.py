"""Helper classes to manage pytorch data."""

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import KW_ONLY, dataclass
from itertools import groupby
from pathlib import Path
from typing import BinaryIO, Generic, NewType, TextIO, TypeAlias, TypeVar, cast

import h5py
import numpy as np
import pandas as pd
import torch
from jaxtyping import Bool, Float, Int
from torch import Tensor
from torch.utils.data import DataLoader, Dataset

_logger = logging.getLogger("stamp")


__author__ = "Marko van Treeck"
__copyright__ = "Copyright (C) 2022-2024 Marko van Treeck"
__license__ = "MIT"


PatientId: TypeAlias = str
GroundTruth: TypeAlias = str
FeaturePath = NewType("FeaturePath", Path)

Category: TypeAlias = str

# One instance
_Bag: TypeAlias = Float[Tensor, "tile feature"]
BagSize: TypeAlias = int
_EncodedTarget: TypeAlias = Bool[Tensor, "category_is_hot"]  # noqa: F821
"""The ground truth, encoded numerically (currently: one-hot)"""

# A batch of the above
Bags: TypeAlias = Float[Tensor, "batch tile feature"]
BagSizes: TypeAlias = Int[Tensor, "batch"]  # noqa: F821
EncodedTargets: TypeAlias = Bool[Tensor, "batch category_is_hot"]
"""The ground truth, encoded numerically (currently: one-hot)"""

PandasLabel: TypeAlias = str

GroundTruthType = TypeVar("GroundTruthType", covariant=True)


@dataclass
class PatientData(Generic[GroundTruthType]):
    """All raw (i.e. non-generated) information we have on the patient."""

    _ = KW_ONLY
    ground_truth: GroundTruthType
    feature_files: Iterable[FeaturePath | BinaryIO]


def dataloader_from_patient_data(
    *,
    patient_data: Sequence[PatientData[GroundTruth | None]],
    bag_size: int | None,
    categories: Sequence[Category] | None = None,
    batch_size: int,
    shuffle: bool,
    num_workers: int,
) -> tuple[DataLoader[tuple[Bags, BagSizes, EncodedTargets]], Sequence[Category]]:
    """Creates a dataloader from patient data, encoding the ground truths.

    Args:
        categories:
            Order of classes for one-hot encoding.
            If `None`, classes are inferred from patient data.
    """

    raw_ground_truths = np.array([patient.ground_truth for patient in patient_data])
    categories = (
        categories if categories is not None else list(np.unique(raw_ground_truths))
    )
    one_hot = torch.tensor(raw_ground_truths.reshape(-1, 1) == categories)
    ds = BagDataset(
        bags=[patient.feature_files for patient in patient_data],
        bag_size=bag_size,
        ground_truths=one_hot,
    )

    return (
        cast(
            DataLoader[tuple[Bags, BagSizes, EncodedTargets]],
            DataLoader(
                ds,
                batch_size=batch_size,
                shuffle=shuffle,
                num_workers=num_workers,
                collate_fn=_collate_to_tuple,
            ),
        ),
        list(categories),
    )


def _collate_to_tuple(
    items: list[tuple[_Bag, BagSize, _EncodedTarget]],
) -> tuple[Bags, BagSizes, EncodedTargets]:
    bags = torch.stack([bag for bag, _, _ in items])
    bag_sizes = torch.tensor([bagsize for _, bagsize, _ in items])
    encoded_targets = torch.stack([encoded_target for _, _, encoded_target in items])

    return (bags, bag_sizes, encoded_targets)


@dataclass
class BagDataset(Dataset[tuple[_Bag, BagSize, _EncodedTarget]]):
    """A dataset of bags of instances."""

    _: KW_ONLY
    bags: Sequence[Iterable[FeaturePath | BinaryIO]]
    """The `.h5` files containing the bags.

    Each bag consists of the features taken from one or multiple h5 files.
    Each of the h5 files needs to have a dataset called `feats` of shape N x F,
    where N is the number of instances and F the number of features per instance.
    """

    bag_size: BagSize | None = None
    """The number of instances in each bag.

    For bags containing more instances,
    a random sample of `bag_size` instances will be drawn.
    Smaller bags are padded with zeros.
    If `bag_size` is None, all the samples will be used.
    """

    ground_truths: Bool[Tensor, "index category_is_hot"]
    """The ground truth for each bag, one-hot encoded."""

    def __post_init__(self) -> None:
        if len(self.bags) != len(self.ground_truths):
            raise ValueError(
                "the number of ground truths has to match the number of bags"
            )

    def __len__(self) -> int:
        return len(self.bags)

    def __getitem__(self, index: int) -> tuple[_Bag, BagSize, _EncodedTarget]:
        # Collect all the features
        feats = []
        for bag_file in self.bags[index]:
            with h5py.File(bag_file, "r") as f:
                feats.append(
                    torch.from_numpy(f["feats"][:])  # pyright: ignore[reportIndexIssue]
                )
        feats = torch.concat(feats).float()

        # Sample a subset, if required
        if self.bag_size:
            return (
                *_to_fixed_size_bag(feats, bag_size=self.bag_size),
                self.ground_truths[index],
            )
        else:
            return (
                feats,
                len(feats),
                self.ground_truths[index],
            )


def _to_fixed_size_bag(bag: _Bag, bag_size: BagSize = 512) -> tuple[_Bag, BagSize]:
    """Samples a fixed-size bag of tiles from an arbitrary one.

    If the original bag did not have enough tiles,
    the bag is zero-padded to the right.
    """
    # get up to bag_size elements
    n_tiles, _dim_feats = bag.shape
    bag_idxs = torch.randperm(n_tiles)[:bag_size]
    bag_samples = bag[bag_idxs]

    # zero-pad if we don't have enough samples
    zero_padded_bag = torch.cat(
        (
            bag_samples,
            torch.zeros(bag_size - bag_samples.shape[0], bag_samples.shape[1]),
        )
    )
    return zero_padded_bag, min(bag_size, len(bag))


def patient_to_ground_truth_from_clini_table_(
    *,
    clini_table_path: Path | TextIO,
    patient_label: PandasLabel,
    ground_truth_label: PandasLabel,
) -> dict[PatientId, GroundTruth]:
    """Loads the patients and their ground truths from a clini table."""
    clini_df = _read_table(
        clini_table_path,
        usecols=[patient_label, ground_truth_label],
        dtype=str,
    ).dropna()
    try:
        patient_to_ground_truth: Mapping[PatientId, GroundTruth] = clini_df.set_index(
            patient_label, verify_integrity=True
        )[ground_truth_label].to_dict()
    except KeyError as e:
        if patient_label not in clini_df:
            raise ValueError(
                f"{patient_label} was not found in clini table "
                f"(columns in clini table: {clini_df.columns})"
            ) from e
        elif ground_truth_label not in clini_df:
            raise ValueError(
                f"{ground_truth_label} was not found in clini table "
                f"(columns in clini table: {clini_df.columns})"
            ) from e
        else:
            raise e from e

    return patient_to_ground_truth


def slide_to_patient_from_slide_table_(
    *,
    slide_table_path: Path,
    feature_dir: Path,
    patient_label: PandasLabel,
    filename_label: PandasLabel,
) -> dict[FeaturePath, PatientId]:
    """Creates a slide-to-patient mapping from a slide table."""
    slide_df = _read_table(
        slide_table_path,
        usecols=[patient_label, filename_label],
        dtype=str,
    )

    slide_to_patient: Mapping[FeaturePath, PatientId] = {
        FeaturePath(feature_dir / cast(str, k)): PatientId(cast(str, patient))
        for k, patient in slide_df.set_index(filename_label, verify_integrity=True)[
            patient_label
        ].items()
    }

    return slide_to_patient


def _read_table(path: Path | TextIO, **kwargs) -> pd.DataFrame:
    if not isinstance(path, Path):
        return pd.read_csv(path, **kwargs)
    elif path.suffix == ".xlsx":
        return pd.read_excel(path, **kwargs)
    elif path.suffix == ".csv":
        return pd.read_csv(path, **kwargs)
    else:
        raise ValueError(
            "table to load has to either be an excel (`*.xlsx`) or csv (`*.csv`) file."
        )


def filter_complete_patient_data_(
    *,
    patient_to_ground_truth: Mapping[PatientId, GroundTruth | None],
    slide_to_patient: Mapping[FeaturePath, PatientId],
    drop_patients_with_missing_ground_truth: bool,
) -> Mapping[PatientId, PatientData]:
    """Aggregate information for all patients for which we have complete data.

    This will sort out slides with missing ground truth, missing features, etc.
    Patients with their ground truth set explicitly set to `None` will be _included_.

    Side effects:
        Checks feature paths' existance.
    """

    _log_patient_slide_feature_inconsitencies(
        patient_to_ground_truth=patient_to_ground_truth,
        slide_to_patient=slide_to_patient,
    )

    patient_to_slides: dict[PatientId, set[FeaturePath]] = {
        patient: set(slides)
        for patient, slides in groupby(
            slide_to_patient, lambda slide: slide_to_patient[slide]
        )
    }

    if not drop_patients_with_missing_ground_truth:
        patient_to_ground_truth = {
            **{patient_id: None for patient_id in patient_to_slides},
            **patient_to_ground_truth,
        }

    patients = {
        patient_id: PatientData(
            ground_truth=ground_truth, feature_files=existing_features_for_patient
        )
        for patient_id, ground_truth in patient_to_ground_truth.items()
        # Restrict to only patients which have slides and features
        if (slides := patient_to_slides.get(patient_id)) is not None
        and (
            existing_features_for_patient := {
                feature_path for feature_path in slides if feature_path.exists()
            }
        )
    }

    return patients


def _log_patient_slide_feature_inconsitencies(
    *,
    patient_to_ground_truth: Mapping[PatientId, GroundTruthType],
    slide_to_patient: Mapping[FeaturePath, PatientId],
) -> None:
    """Checks whether the arguments are consistent and logs all irregularities.

    Has no side effects outside of logging.
    """
    if (
        patients_without_slides := patient_to_ground_truth.keys()
        - slide_to_patient.values()
    ):
        _logger.warning(
            f"some patients have no associated slides: {patients_without_slides}"
        )

    if patients_without_ground_truth := (
        slide_to_patient.values() - patient_to_ground_truth.keys()
    ):
        _logger.warning(
            f"some patients have no clinical information: {patients_without_ground_truth}"
        )

    if slides_without_features := {
        slide for slide in slide_to_patient.keys() if not slide.exists()
    }:
        _logger.warning(
            f"some feature files could not be found: {slides_without_features}"
        )
