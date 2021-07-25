#!/usr/bin/env python3
"""
Pre-processing pipeline for Google Speech Commands
"""
import os
import re
from pathlib import Path
from typing import List

import luigi
import pandas as pd
import soundfile as sf
from tqdm import tqdm
from slugify import slugify

import heareval.tasks.pipeline as pipeline
import heareval.tasks.util.luigi as luigi_util

WORDS = ["down", "go", "left", "no", "off", "on", "right", "stop", "up", "yes"]
BACKGROUND_NOISE = "_background_noise_"
UNKNOWN = "_unknown_"
SILENCE = "_silence_"


config = {
    "task_name": "speech_commands",
    "version": "v0.0.2",
    "task_type": "scene_labeling",
    "download_urls": {
        "train": "http://download.tensorflow.org/data/speech_commands_v0.02.tar.gz",  # noqa: E501
        "test": "http://download.tensorflow.org/data/speech_commands_test_set_v0.02.tar.gz",  # noqa: E501
    },
    "sample_duration": 1.0,
    "splits": [
        {"name": "train", "max_files": 100},
        {"name": "test", "max_files": 100},
        {"name": "valid", "max_files": 100},
    ],
}


class GenerateTrainDataset(luigi_util.WorkTask):
    """
    Silence / background samples in the train / validation sets need to be
    created by slicing up longer background samples into 1sec slices.
    This is the same method used in the TensorFlow dataset generator.
    https://github.com/tensorflow/datasets/blob/79d56e662a15cd11e1fb3b679e0f978c8041566f/tensorflow_datasets/audio/speech_commands.py#L142
    """

    # Requires an extracted dataset task to be completed
    train_data = luigi.TaskParameter()

    def requires(self):
        return {"train": self.train_data}

    def run(self):
        train_path = Path(self.requires()["train"].workdir).joinpath("train")
        background_audio = list(train_path.glob(f"{BACKGROUND_NOISE}/*.wav"))
        assert len(background_audio) > 0

        # Read all the background audio files and split into 1 second segments,
        # save all the segments into a folder called _silence_
        silence_dir = os.path.join(self.workdir, SILENCE)
        os.makedirs(silence_dir, exist_ok=True)

        print("Generating silence files from background sounds ...")
        for audio_path in tqdm(background_audio):
            audio, sr = sf.read(str(audio_path))

            basename = os.path.basename(audio_path)
            name, ext = os.path.splitext(basename)

            for start in range(0, len(audio) - sr, sr // 2):
                audio_segment = audio[start : start + sr]
                filename = f"{name}-{start}{ext}"
                filename = os.path.join(silence_dir, filename)
                sf.write(filename, audio_segment, sr)

        # We'll also create symlinks for the dataset here too to make the next
        # stage of splitting into training and validation files easier.
        for file_obj in train_path.iterdir():
            if file_obj.is_dir() and file_obj.name != BACKGROUND_NOISE:
                linked_folder = Path(os.path.join(self.workdir, file_obj.name))
                linked_folder.unlink(missing_ok=True)
                linked_folder.symlink_to(file_obj.absolute(), target_is_directory=True)

            # Also need the testing and validation splits
            if file_obj.name in ["testing_list.txt", "validation_list.txt"]:
                linked_file = Path(os.path.join(self.workdir, file_obj.name))
                linked_file.unlink(missing_ok=True)
                linked_file.symlink_to(file_obj.absolute())

        self.mark_complete()


class ExtractMetadata(pipeline.ExtractMetadata):
    train = luigi.TaskParameter()
    test = luigi.TaskParameter()

    def requires(self):
        return {
            "train": self.train,
            "test": self.test,
        }

    @staticmethod
    def apply_label(relative_path):
        label = os.path.basename(os.path.dirname(relative_path))
        if label not in WORDS and label != SILENCE:
            label = UNKNOWN
        return label

    @staticmethod
    def slugify_file_name(relative_path: str) -> str:
        """
        For speech command each speaker might have given samples for
        different labels. In this case, just sluggifying the file name
        without the label would cause duplicates
        """
        # Get the foldername which is the label and the filename
        name = os.path.splitext(os.path.join(*Path(relative_path).parts[-2:]))[0]
        return f"{slugify(str(name))}"

    @staticmethod
    def get_subsample_key(slug: str):
        # Speaker hash is a unique hash at a speaker level.
        # This is generated by removing the part of the file starting at -nohash-
        speaker_hash = luigi_util.filename_to_int_hash(re.sub(r"-nohash-.*$", "", slug))
        # Filename hash is a unique hash at a file level.
        filename_hash = luigi_util.filename_to_int_hash(slug)
        # This way while subsampling the audio clips, clips by a single
        # speaker will either be selected or not in a group followed by
        # selection on filename_hash to tie break among a speaker group.
        subsample_key = (speaker_hash, filename_hash)
        return subsample_key

    def get_split_paths(self):
        """
        Splits the dataset into train/valid/test files using the same method as
        described in by the TensorFlow dataset:
        https://www.tensorflow.org/datasets/catalog/speech_commands
        """
        # Test files
        test_path = Path(self.requires()["test"].workdir).joinpath("test")
        test_df = pd.DataFrame(test_path.glob("*/*.wav"), columns=["relpath"]).assign(
            split=lambda df: "test"
        )

        # All silence paths to add to the train and validation
        train_path = Path(self.requires()["train"].workdir)
        all_silence = list(train_path.glob(f"{SILENCE}/*.wav"))

        # Validation files
        with open(os.path.join(train_path, "validation_list.txt"), "r") as fp:
            validation_paths = fp.read().strip().splitlines()
        validation_rel_paths = [os.path.join(train_path, p) for p in validation_paths]

        # There are no silence files marked explicitly for validation. We add all
        # the running_tap.wav samples to the silence class for validation.
        # https://github.com/tensorflow/datasets/blob/e24fe9e6b03053d9b925d299a2246ea167dc85cd/tensorflow_datasets/audio/speech_commands.py#L183
        val_silence = list(train_path.glob(f"{SILENCE}/running_tap*.wav"))
        validation_rel_paths.extend(val_silence)
        validation_df = pd.DataFrame(validation_rel_paths, columns=["relpath"]).assign(
            split=lambda df: "valid"
        )

        # Train-test files.
        with open(os.path.join(train_path, "testing_list.txt"), "r") as fp:
            train_test_paths = fp.read().strip().splitlines()
        audio_paths = [
            str(p.relative_to(train_path)) for p in train_path.glob("[!_]*/*.wav")
        ]

        # The final train set is all the audio files MINUS the files marked as
        # test / validation files in testing_list.txt or validation_list.txt
        train_paths = list(
            set(audio_paths) - set(train_test_paths) - set(validation_paths)
        )
        train_rel_paths = [os.path.join(train_path, p) for p in train_paths]

        # Training silence is all the generated silence / background noise samples
        # minus those marked for validation.
        train_silence = list(set(all_silence) - set(val_silence))
        train_rel_paths.extend(train_silence)
        train_df = pd.DataFrame(train_rel_paths, columns=["relpath"]).assign(
            split=lambda df: "train"
        )
        assert len(train_df.merge(validation_df, on="relpath")) == 0

        return pd.concat([test_df, validation_df, train_df])

    def get_process_metadata(self) -> pd.DataFrame:
        process_metadata = self.get_split_paths()
        process_metadata = process_metadata.assign(
            slug=lambda df: df["relpath"].apply(self.slugify_file_name),
            subsample_key=lambda df: df["slug"].apply(self.get_subsample_key),
            label=lambda df: df["relpath"].apply(self.apply_label),
        )
        return process_metadata


def main(num_workers: int, sample_rates: List[int]):

    download_tasks = pipeline.get_download_and_extract_tasks(config)

    generate = GenerateTrainDataset(
        train_data=download_tasks["train"], data_config=config
    )
    configure_metadata = ExtractMetadata(
        train=generate,
        test=download_tasks["test"],
        outfile="process_metadata.csv",
        data_config=config,
    )

    final = pipeline.FinalizeCorpus(
        sample_rates=sample_rates, metadata=configure_metadata, data_config=config
    )

    pipeline.run(final, num_workers=num_workers)
