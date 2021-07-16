"""
This script builds a `benchmark.md` file which aggregates the results saved on disk.

It searches for all results in a root directory, computes the trajectory error, ranks the results,
And display writes the `benchmark.md` files which contains the table aggregating all the results.
"""
import sys
import os
from dataclasses import dataclass
import logging
from pathlib import Path
import yaml

import hydra
from hydra.core.config_store import ConfigStore

from slam.common.utils import assert_debug
from slam.common.io import *
from slam.eval.eval_odometry import *


@dataclass
class BenchmarkBuilderConfig:
    root_dir: str = "."
    dataset: str = "kitti"
    output_dir: str = ".benchmark"


cs = ConfigStore.instance()
cs.store(name="benchmark", node=BenchmarkBuilderConfig)


@hydra.main(config_path=None, config_name="benchmark")
def build_benchmark(cfg: BenchmarkBuilderConfig) -> None:
    """Builds the benchmark"""

    root_dir = cfg.root_dir
    dataset = cfg.dataset
    assert_debug(dataset == "kitti", "Only kitti is supported (for now) to build a benchmark")
    folder_names = [f"{i:02}" for i in range(11)]

    metrics = {}  # map root_path -> computed metrics
    # Recursively search all child directories for folder with the appropriate name
    directory_list = [x[0] for x in os.walk(root_dir)]  # Absolute paths

    for new_dir in directory_list:

        is_metrics_dir = False
        new_dir_path = Path(new_dir)
        for folder in folder_names:
            if (new_dir_path / folder).exists():
                is_metrics_dir = True

        if is_metrics_dir:
            # New entry found compute and add the metrics for each sequence
            new_metrics = {}
            has_all_sequences = True
            for sequence_name in folder_names:
                sequence_path = new_dir_path / sequence_name

                poses_file = sequence_path / f"{sequence_name}.poses.txt"
                gt_poses_file = sequence_path / f"{sequence_name}_gt.poses.txt"
                if poses_file.exists() and gt_poses_file.exists():
                    if not new_metrics:
                        print(f"[INFO]Found a results directory at {new_dir}")

                    print(f"[INFO]Computing trajectory error for sequence {sequence_name}")
                    # Can compute metrics on both files
                    gt_poses = read_poses_from_disk(gt_poses_file)
                    poses = read_poses_from_disk(poses_file)

                    # Try to read the configuration files and metrics
                    metrics_yaml = sequence_path / "metrics.yaml"
                    time_ms = -1.0
                    if metrics_yaml.exists():
                        with open(str(metrics_yaml), "r") as stream:
                            metrics_dict = yaml.safe_load(stream)
                            if sequence_name in metrics_dict and "nsecs_per_frame" in metrics_dict[sequence_name]:
                                time_ms = float(metrics_dict[sequence_name]["nsecs_per_frame"]) * 1000.0

                    tr_err, rot_err, errors = compute_kitti_metrics(poses, gt_poses)

                    new_metrics[sequence_name] = {
                        "tr_err": tr_err,
                        "rot_err": rot_err,
                        "errors": errors,
                        "average_time": time_ms
                    }
                else:
                    has_all_sequences = False

            if new_metrics:
                if has_all_sequences:
                    # Compute the average errors
                    _errors = []
                    for seq_metrics in new_metrics.values():
                        _errors += seq_metrics["errors"]
                    avg_tr_err = sum([error["tr_err"][0] for error in _errors]) / len(_errors)
                    new_metrics["AVG_tr_err"] = avg_tr_err * 100
                    new_metrics["AVG_time"] = sum([new_metrics[seq]["average_time"] for seq in folder_names]) / len(
                        folder_names)
                else:
                    new_metrics["AVG_tr_err"] = -1.0
                    new_metrics["AVG_time"] = -1.0

                new_metrics["has_all_sequences"] = has_all_sequences
                metrics[new_dir] = new_metrics

                # Try and load the overrides
                overrides_file = new_dir_path / ".hydra" / "overrides.yaml"
                if overrides_file.exists():
                    with open(str(overrides_file), "r") as stream:
                        overrides_list = yaml.safe_load(stream)
                        command_line = "`python run.py " + " ".join(overrides_list) + "`"
                        new_metrics["command"] = command_line

    # Build the benchmark.md table
    db_metrics = [(path,
                   entry_metrics["AVG_tr_err"],
                   entry_metrics["has_all_sequences"]) for path, entry_metrics in metrics.items()]
    db_metrics.sort(key=lambda x: x[1] if x[2] else float("inf"))

    # Build the list
    header = ["## KITTI Benchmark:\n\n\n"]
    main_table_lines = ["#### Sorted trajectory error on all sequences:\n",
                        f"| **Sequence Folder**|{' | '.join(folder_names)}  |  AVG  | AVG Time (ms) |\n",
                        "| ---: " * (len(folder_names) + 3) + "|\n"]

    command_lines = ["#### Command Lines for each entry\n",
                     f"| **Sequence Folder** | Command Line |\n",
                     "| ---: | ---: |\n"]

    for entry in db_metrics:
        path, avg, add_avg = entry
        path_id = os.path.split(path)[1]
        _metrics = metrics[path]
        avg_time = _metrics["AVG_time"]
        columns = ' | '.join(
            [f"{float(_metrics[seq]['tr_err']) * 100:.4f}" if seq in _metrics else '' for seq in folder_names])

        path_link = f"[{path_id}]({str(Path(path).resolve())})"
        line = f"| {path_link} | {columns} | {f'{avg:.4f}' if add_avg else ''} | {f'{avg_time:.3f}'} |\n"
        main_table_lines.append(line)

        command_lines.append(f"| {path_link} |  {_metrics['command'] if 'command' in _metrics else ''}   |\n")

    output_root = Path(cfg.output_dir)
    if not output_root.exists():
        output_root.mkdir()

    output_file = str(output_root / "benchmark.md")
    with open(output_file, "w") as stream:
        stream.writelines(header)
        stream.writelines(main_table_lines)
        stream.writelines(command_lines)


if __name__ == "__main__":
    # Set the working directory to current directory
    sys.argv.append(f'hydra.run.dir={os.getcwd()}')
# Disable logging
sys.argv.append("hydra/hydra_logging=disabled")
sys.argv.append("hydra/job_logging=disabled")
sys.argv.append("hydra.output_subdir=null")
build_benchmark()
