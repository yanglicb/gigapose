import numpy as np
import os.path as osp
import os
import pytorch_lightning as pl
from pathlib import Path
from megapose.utils.tensor_collection import PandasTensorCollection
from megapose.inference.types import ObservationTensor
from src.utils.logging import get_logger
from src.custom_megapose.refiner_utils import load_pretrained_refiner
from src.utils.inout import save_predictions_from_batched_predictions
from src.utils.time import Timer

logger = get_logger(__name__)


class Refiner(pl.LightningModule):
    def __init__(
        self,
        object_dataset,
        cfg_refiner_model,
        use_multiple,
        log_dir,
        test_dataset_name,
        coarse_model_name,
        run_id,
        **kwargs,
    ):
        # define the network
        super().__init__()
        self.use_multiple = use_multiple
        self.n_iterations = cfg_refiner_model.n_iterations
        self.pose_estimator = load_pretrained_refiner(cfg_refiner_model, object_dataset)

        self.log_dir = Path(log_dir)
        self.test_dataset_name = test_dataset_name
        self.coarse_model_name = coarse_model_name
        self.run_id = run_id
        self.use_average_score = True
        self.timer = Timer()
        os.makedirs(self.log_dir, exist_ok=True)

        if self.use_multiple:
            self.refined_predictions_dir = self.log_dir / "refined_multiple_predictions"
        else:
            self.refined_predictions_dir = self.log_dir / "refined_predictions"
        os.makedirs(self.refined_predictions_dir, exist_ok=True)
        logger.info("Init Refiner done!")

    def move_to_device(self):
        self.pose_estimator.coarse_model.mesh_db.to(self.device)
        self.pose_estimator.coarse_model.to(self.device)
        self.pose_estimator.coarse_model.eval()

        self.pose_estimator.refiner_model.mesh_db.to(self.device)
        self.pose_estimator.refiner_model.to(self.device)
        self.pose_estimator.refiner_model.eval()

        self.pose_estimator.to(self.device)
        logger.info(f"Moving models to {self.device} done!")

    def test_step(self, batch, idx_batch):
        if idx_batch == 0:
            self.move_to_device()

        observation = ObservationTensor(images=batch.rgb, K=batch.K)
        data_TCO = PandasTensorCollection(
            infos=batch.infos,
            poses=batch.TCO_init,
        )

        self.timer.tic()
        preds, refiner_extra_data = self.pose_estimator.forward_refiner(
            observation=observation,
            data_TCO_input=data_TCO,
            n_iterations=self.n_iterations,
            keep_all_outputs=False,
            cuda_timer=None,
        )

        data_TCO_refined = preds[f"iteration={self.n_iterations}"]
        (
            data_TCO_scored,
            scoring_extra_data,
        ) = self.pose_estimator.forward_scoring_model(
            observation,
            data_TCO_refined,
        )

        # Extract the highest scoring pose estimate for each instance_id
        data_TCO_final_scored = self.pose_estimator.filter_pose_estimates(
            data_TCO_scored, top_K=1, filter_field="pose_logit"
        )
        if self.use_average_score:
            data_TCO_final_scored.infos.pose_score = (
                data_TCO_final_scored.infos.matching_score
                + data_TCO_final_scored.infos.pose_score
            ) / 2

        pred_poses = data_TCO_final_scored.poses
        pred_poses[:, :3, 3] *= 1000  # convert to mm
        obj_id = data_TCO_final_scored.infos.label
        obj_id = [int(i.split("_")[1]) for i in obj_id]
        refinement_time = self.timer.toc()
        self.timer.reset()
        save_path = osp.join(
            self.refined_predictions_dir, f"batch_{idx_batch:06d}.npz"
        )

        np.savez(
            save_path,
            scene_id=data_TCO_final_scored.infos.scene_id,
            im_id=data_TCO_final_scored.infos.im_id,
            object_id=np.array(obj_id),
            poses=pred_poses.cpu().numpy(),
            scores=data_TCO_final_scored.infos.pose_score,
            time=data_TCO_final_scored.infos.time,
            refinement_time=np.array([refinement_time for _ in range(len(obj_id))]),
        )
        if idx_batch % 20 == 0:
            logger.info(f"Refining tooks {refinement_time} s")
        return 0

    def on_test_epoch_end(self):
        if self.global_rank == 0:
            self.pose_estimator.refiner_model.renderer.stop()
            save_predictions_from_batched_predictions(
                self.refined_predictions_dir,
                dataset_name=self.test_dataset_name,
                model_name=f"{self.coarse_model_name}",
                run_id=self.run_id,
                is_refined=True,
            )
