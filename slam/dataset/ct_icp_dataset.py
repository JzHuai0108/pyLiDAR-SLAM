from slam.common.modules import _with_ct_icp

if _with_ct_icp:
    import pyct_icp as pct

    import logging
    from pathlib import Path

    import numpy as np
    from torch.utils.data import Dataset

    # Hydra and OmegaConf
    from hydra.conf import dataclass, field
    from hydra.core.config_store import ConfigStore
    from omegaconf import MISSING

    # Project Imports
    from slam.eval.eval_odometry import compute_relative_poses
    from slam.dataset import DatasetConfig, DatasetLoader
    from slam.common.projection import SphericalProjector
    from slam.common.utils import assert_debug
    from slam.odometry.ct_icp_odometry import add_pct_annotations, CT_ICPOdometry


    @dataclass
    @add_pct_annotations(pct.DatasetOptions)
    class CT_ICPDatasetOptionsWrapper:
        """A dataclass wrapper for a pct.DatasetOptions

        The fields of the dataclass are programmatically defined from the attributes of a pct.DatasetOptions
        """

        def to_pct_object(self):
            options = pct.DatasetOptions()
            for field_name in self.__dict__:
                if field_name == "dataset":
                    field_value = getattr(self, field_name)
                    field_value = getattr(pct.CT_ICP_DATASET, field_value)
                    setattr(options, field_name, field_value)
                else:
                    field_value = getattr(self, field_name)
                    assert_debug(field_value != MISSING)
                    setattr(options, field_name, field_value)

            return options


    @dataclass
    class CT_ICPDatasetConfig(DatasetConfig):
        """A configuration object read from a yaml conf"""
        # -------------------
        # Required Parameters
        dataset: str = "ct_icp"

        options: CT_ICPDatasetOptionsWrapper = field(default_factory=lambda: CT_ICPDatasetOptionsWrapper())

        # ------------------------------
        # Parameters with default values
        lidar_key: str = "vertex_map"
        lidar_height: int = 64
        lidar_width: int = 1024
        up_fov: int = 3
        down_fov: int = -24
        all_sequence: list = field(default_factory=lambda: [f"{i:02}" for i in range(11) if i != 3] +
                                                           [f"Town{1 + i:02}" for i in range(7)])
        train_sequences: list = field(default_factory=lambda: [f"{i:02}" for i in range(11) if i != 3] +
                                                              [f"Town{1 + i:02}" for i in range(7)])
        test_sequences: list = field(default_factory=lambda: [f"{i:02}" for i in range(22) if i != 3])
        eval_sequences: list = field(default_factory=lambda: ["09", "10"])


    # Hydra -- stores a KITTIConfig `ct_icp` in the `dataset` group
    cs = ConfigStore.instance()
    cs.store(group="dataset", name="ct_icp", node=CT_ICPDatasetConfig)


    class CT_ICPDatasetSequence(Dataset):
        """
        Dataset for a Sequence defined in CT_ICP Datasets
        See https://github.com/jedeschaud/ct_icp for more details

        Attributes:
            options (CT_ICPDatasetOptionsWrapper): the ct_icp options to load the dataset
            sequence_id (str): id of the sequence
        """

        def __init__(self,
                     options: pct.DatasetOptions,
                     sequence_id: int,
                     gt_pose_channel: str = "absolute_pose_gt",
                     numpy_pc_channel: str = "numpy_pc"):
            assert isinstance(options, pct.DatasetOptions)
            self.options: pct.DatasetOptions = options
            assert_debug(self.options.dataset != pct.NCLT, "The NCLT Dataset is not available in Random Access")
            self.dataset_sequences = pct.get_dataset_sequence(self.options, sequence_id)
            self.sequence_id = sequence_id
            self.gt_pose_channel = gt_pose_channel
            self.numpy_pc_channel = numpy_pc_channel

            self.gt = None
            if pct.has_ground_truth(options, sequence_id):
                self.gt = np.array(pct.load_sensor_ground_truth(options, sequence_id), np.float64)

        def __len__(self):
            return self.dataset_sequences.NumFrames()

        def __getitem__(self, idx) -> dict:
            assert_debug(0 <= idx < len(self), "Index Error")
            lidar_frame = self.dataset_sequences.Frame(idx)

            data_dict = dict()

            # Add numpy pc values
            lidar_frame_ref = lidar_frame.GetStructuredArrayRef()
            numpy_pc = lidar_frame_ref["raw_point"].copy()
            timestamps = lidar_frame_ref["timestamp"].copy()

            data_dict[self.numpy_pc_channel] = numpy_pc.astype(np.float32)
            data_dict[f"{self.numpy_pc_channel}_timestamps"] = timestamps

            if self.gt is not None:
                data_dict[f"{self.gt_pose_channel}"] = self.gt[idx]

            return data_dict


    class CT_ICPDatasetLoader(DatasetLoader):
        """
        Configuration for a dataset proposed in CT_ICP
        """

        __KITTI_SEQUENCE = [f"{i:02}" for i in range(22) if i != 3]
        __KITTI_CARLA_SEQUENCE = [f"Town{1 + i:02}" for i in range(7)]

        @staticmethod
        def have_sequence(seq_name):
            return seq_name in CT_ICPDatasetLoader.__KITTI_SEQUENCE or \
                   seq_name in CT_ICPDatasetLoader.__KITTI_CARLA_SEQUENCE

        def __init__(self, config: CT_ICPDatasetConfig):
            super().__init__(config)
            self.options: pct.DatasetOptions = CT_ICPDatasetOptionsWrapper(**config.options).to_pct_object()

            root_path = Path(self.options.root_path)
            assert_debug(root_path.exists(), f"The root path of the dataset {str(root_path)} does not exist on disk")

            # Build the dictionary sequence_name -> sequence_id
            self.map_seqname_seqid = dict()
            all_sequences_id_size = pct.get_sequences(self.options)
            for seq_id, seq_size in all_sequences_id_size:
                seq_name = pct.sequence_name(self.options, seq_id)
                assert_debug(seq_name in self.__KITTI_SEQUENCE or seq_name in self.__KITTI_CARLA_SEQUENCE)
                self.map_seqname_seqid[seq_name] = seq_id

        def projector(self) -> SphericalProjector:
            """Default SphericalProjetor for KITTI (projection of a pointcloud into a Vertex Map)"""
            assert isinstance(self.config, CT_ICPDatasetConfig)
            lidar_height = self.config.lidar_height
            lidar_with = self.config.lidar_width
            up_fov = self.config.up_fov
            down_fov = self.config.down_fov
            # Vertex map projector
            projector = SphericalProjector(lidar_height, lidar_with, 3, up_fov, down_fov)
            return projector

        def get_ground_truth(self, sequence_name):
            """Returns the ground truth poses associated to a sequence of KITTI's odometry benchmark"""
            assert_debug(sequence_name in self.map_seqname_seqid)
            seq_id = self.map_seqname_seqid[sequence_name]
            ground_truth = pct.load_sensor_ground_truth(self.options, seq_id)
            absolute_poses = np.array(ground_truth).astype(np.float64)
            return compute_relative_poses(absolute_poses)

        def sequences(self):
            """
            Returns
            -------
            (train_dataset, eval_dataset, test_dataset, transform) : tuple
            train_dataset : (list, list)
                A list of dataset_config (one for each sequence of KITTI's Dataset),
                And the list of sequences used to build them
            eval_dataset : (list, list)
                idem
            test_dataset : (list, list)
                idem
            transform : callable
                A transform to be applied on the dataset_config
            """
            assert isinstance(self.config, CT_ICPDatasetConfig)

            # Sets the path of the kitti benchmark
            train_sequence_ids = self.config.train_sequences
            eval_sequence_ids = self.config.eval_sequences
            test_sequence_ids = self.config.test_sequences

            list_seqid_seqsize = pct.get_sequences(self.options)
            seqname_to_seqid = {pct.sequence_name(self.options, seq_id): seq_id for seq_id, _ in list_seqid_seqsize}

            _options = self.options

            def __get_datasets(sequences: list):
                if sequences is None or len(sequences) == 0:
                    return None

                datasets = []
                sequence_names = []
                for seq_name in sequences:
                    if not self.have_sequence(seq_name) or seq_name not in seqname_to_seqid:
                        logging.warning(
                            f"The dataset located at {_options.root_path} does not have the sequence named {seq_name}")
                        continue
                    seq_id = seqname_to_seqid[seq_name]
                    datasets.append(CT_ICPDatasetSequence(_options, seq_id))
                    sequence_names.append(seq_name)

                return datasets, sequence_names

            return __get_datasets(train_sequence_ids), \
                   __get_datasets(eval_sequence_ids), \
                   __get_datasets(test_sequence_ids), lambda x: x
