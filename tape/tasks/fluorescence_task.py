from typing import Union, Dict, List, Type
import math
from pathlib import Path
from argparse import ArgumentParser, Namespace

import scipy.stats
import torch
import torch.nn as nn
import pytorch_lightning as pl
from Bio.Align import PairwiseAligner, substitution_matrices
from tqdm import trange

from ..models.modeling_utils import ProteinModel
from ..tokenizers import TAPETokenizer
from ..utils import (
    pad_sequences,
    TensorDict,
    seqlen_mask,
    parse_fasta,
    hhfilter_sequences,
)
from .tape_task import (
    TAPEDataset,
    TAPEDataModule,
    TAPEPredictorBase,
    TAPETask,
)


class FluorescenceDataset(TAPEDataset):
    def __init__(
        self,
        data_path: Union[str, Path],
        split: str,
        tokenizer: Union[str, TAPETokenizer] = "iupac",
        use_msa: bool = False,
        max_tokens_per_msa: int = 2 ** 14,
        return_cached_embeddings: bool = True,
    ):
        super().__init__(
            data_path=data_path,
            split=split,
            tokenizer=tokenizer,
            use_msa=use_msa,
            max_tokens_per_msa=max_tokens_per_msa,
        )
        self.return_cached_embeddings = return_cached_embeddings
        if self.use_msa:
            msa_file = "fluorescence/wtGFP.a3m"
            msa_path = self.data_path / msa_file
            _, msa = parse_fasta(msa_path, remove_insertions=True)
            reference = msa[0]
            seqlen = len(self.tokenizer.encode(self.data[0]["primary"]))
            max_num_sequences = max_tokens_per_msa // seqlen
            sequences_from_msa = max_num_sequences - 1

            sequences = hhfilter_sequences(msa, diff=sequences_from_msa)
            tokens = torch.stack(
                [
                    torch.from_numpy(self.tokenizer.encode(seq))
                    for _, seq in sequences[:sequences_from_msa]
                ],
                0,
            ).long()
            self.reference = reference
            self.msa = tokens
            self.aligner = PairwiseAligner(
                substitution_matrix=substitution_matrices.load("BLOSUM62"),
                mode="global",
                target_open_gap_score=-float("inf"),
            )

    def maybe_align_sequence(self, sequence: str) -> str:
        if not self.use_msa or len(sequence) == len(self.reference):
            return sequence
        else:
            return str(self.aligner.align(self.reference, sequence)[0]).split("\n")[2]

    @property
    def task_name(self) -> str:
        return "fluorescence"

    @property
    def splits(self) -> List[str]:
        return ["train", "valid", "test"]

    def __getitem__(self, index: int):
        item = self.data[index]
        sequence = self.maybe_align_sequence(item["primary"])
        src_tokens = torch.from_numpy(self.tokenizer.encode(sequence)).long()
        if self.use_msa:
            src_tokens = src_tokens.unsqueeze(0)
            src_tokens = torch.cat([src_tokens, self.msa], 0)
        value = torch.tensor(item["log_fluorescence"][0], dtype=torch.float)
        result = {"src_tokens": src_tokens, "log_fluorescence": value}
        if self.return_cached_embeddings:
            embed = self.load_cached_embedding(index)
            result["features"] = embed
        return result

    def collate_fn(
        self, batch: List[TensorDict]
    ) -> Dict[str, Union[torch.Tensor, TensorDict]]:
        src_tokens = pad_sequences([el["src_tokens"] for el in batch], self.pad_idx)
        src_lengths = torch.tensor(
            [el["src_tokens"].size(-1) for el in batch], dtype=torch.long
        )

        result = {
            "net_input": {
                "src_tokens": src_tokens,
                "src_lengths": src_lengths,
            },
            "log_fluorescence": torch.stack(
                [item["log_fluorescence"] for item in batch], 0
            ).unsqueeze(1),
        }
        if self.return_cached_embeddings:
            result["net_input"]["features"] = pad_sequences(  # type: ignore
                [el["features"] for el in batch], 0
            )
        return result  # type: ignore

    def cached_embedding_path(self, index: int) -> Path:
        embedding_dir = self.data_path / self.task_name / "embeddings"
        embedding_dir.mkdir(exist_ok=True)
        name = f"{self.split}_{index}{'_msa' if self.use_msa else ''}.pt"
        path = embedding_dir / name
        return path

    def load_cached_embedding(self, index: int) -> torch.Tensor:
        path = self.cached_embedding_path(index)
        embed = torch.load(path)
        embed.requires_grad_(False)
        return embed

    def make_embedding(self, base_model: ProteinModel, index: int):
        device = next(base_model.parameters()).device
        path = self.cached_embedding_path(index)
        if not path.exists():
            net_input: Dict[str, torch.Tensor] = self.collate_fn(  # type: ignore
                [self[index]]
            )["net_input"]
            for key, value in net_input.items():
                net_input[key] = value.to(device)
            embed = base_model.extract_features(**net_input)[0]
            embed = embed.cpu().clone()
            torch.save(embed, str(path))

    def make_all_embeddings(self, base_model: ProteinModel):
        for index in trange(len(self)):
            self.make_embedding(base_model, index)


class FluorescenceDataModule(TAPEDataModule):
    @property
    def data_url(self) -> str:
        return "http://s3.amazonaws.com/proteindata/data_pytorch/fluorescence.tar.gz"  # noqa: E501

    @property
    def task_name(self) -> str:
        return "fluorescence"

    @property
    def dataset_type(self) -> Type[FluorescenceDataset]:
        return FluorescenceDataset


class FluorescencePredictor(TAPEPredictorBase):
    def __init__(
        self,
        base_model: ProteinModel,
        freeze_base: bool = False,
        optimizer: str = "adam",
        learning_rate: float = 1e-3,
        weight_decay: float = 1e-4,
        lr_scheduler: str = "constant",
        warmup_steps: int = 0,
        max_steps: int = 10000,
        dropout: float = 0.1,
        hidden_size: int = 512,
    ):
        super().__init__(
            base_model=base_model,
            freeze_base=freeze_base,
            optimizer=optimizer,
            learning_rate=learning_rate,
            weight_decay=weight_decay,
            lr_scheduler=lr_scheduler,
            warmup_steps=warmup_steps,
            max_steps=max_steps,
        )
        self.save_hyperparameters(
            "dropout",
            "hidden_size",
        )

        self.compute_attention_weights = nn.Linear(self.embedding_dim, 1)
        self.dropout = nn.Dropout(dropout)
        self.mlp = nn.Sequential(
            nn.Linear(self.embedding_dim, hidden_size),
            nn.ReLU(),
            nn.Linear(hidden_size, 1),
        )

        self.mean_train_squared_error = pl.metrics.MeanSquaredError()
        self.mean_train_absolute_error = pl.metrics.MeanAbsoluteError()
        self.mean_valid_squared_error = pl.metrics.MeanSquaredError()
        self.mean_valid_absolute_error = pl.metrics.MeanAbsoluteError()
        self.mean_test_squared_error = pl.metrics.MeanSquaredError()
        self.mean_test_absolute_error = pl.metrics.MeanAbsoluteError()

    @staticmethod
    def add_args(parser: ArgumentParser) -> ArgumentParser:
        parser = TAPEPredictorBase.add_args(parser)
        parser.add_argument(
            "--dropout",
            type=float,
            default=0.1,
            help="Dropout on attention block.",
        )
        parser.add_argument(
            "--hidden_size",
            type=int,
            default=512,
            help="MLP hidden dimension.",
        )
        return parser

    def forward(self, src_tokens, src_lengths, features=None):
        # B x L x D
        if features is None:
            features = self.base_model.extract_features(src_tokens, src_lengths)
        attention_weights = self.compute_attention_weights(features)
        attention_weights /= math.sqrt(features.size(2))
        mask = seqlen_mask(
            features, src_lengths - (src_tokens.size(1) - features.size(1))
        )
        attention_weights = attention_weights.masked_fill(~mask.unsqueeze(2), -10000)
        attention_weights = attention_weights.softmax(1)
        attention_weights = self.dropout(attention_weights)
        pooled_features = features.transpose(1, 2) @ attention_weights
        pooled_features = pooled_features.squeeze(2)
        return self.mlp(pooled_features)

    def compute_loss(self, batch, mode: str):
        log_fluorescence = self(**batch["net_input"])
        loss = nn.MSELoss()(log_fluorescence, batch["log_fluorescence"])
        self.log(f"loss/{mode}", loss)

        return {"loss": loss, "log_fluorescence": log_fluorescence, "target": batch}

    def compute_and_log_accuracy(self, outputs, mode: str):
        mse = getattr(self, f"mean_{mode}_squared_error")
        mae = getattr(self, f"mean_{mode}_absolute_error")
        mse(outputs["log_fluorescence"], outputs["target"]["log_fluorescence"])
        mae(outputs["log_fluorescence"], outputs["target"]["log_fluorescence"])
        self.log(f"mse/{mode}", mse)
        self.log(f"mae/{mode}", mae)

    def training_step(self, batch, batch_idx):
        return self.compute_loss(batch, "train")

    def training_step_end(self, outputs):
        self.compute_and_log_accuracy(outputs, "train")
        return outputs

    def validation_step(self, batch, batch_idx):
        return self.compute_loss(batch, "valid")

    def validation_step_end(self, outputs):
        self.compute_and_log_accuracy(outputs, "valid")
        return outputs

    def validation_epoch_end(self, outputs):
        predictions = torch.cat([step["log_fluorescence"] for step in outputs], 0)
        targets = torch.cat([step["target"]["log_fluorescence"] for step in outputs], 0)
        corr, _ = scipy.stats.spearmanr(predictions.cpu(), targets.cpu())
        self.log("spearmanr/valid", corr)

    def test_step(self, batch, batch_idx):
        return self.compute_loss(batch, "test")

    def test_step_end(self, outputs):
        self.compute_and_log_accuracy(outputs, "test")
        return outputs

    def test_epoch_end(self, outputs):
        predictions = torch.cat([step["log_fluorescence"] for step in outputs], 0)
        targets = torch.cat([step["target"]["log_fluorescence"] for step in outputs], 0)
        corr, _ = scipy.stats.spearmanr(predictions.cpu(), targets.cpu())
        self.log("spearmanr/test", corr)

    @classmethod
    def from_argparse_args(
        cls,
        args: Namespace,
        base_model: ProteinModel,
    ):
        return cls(
            base_model=base_model,
            freeze_base=args.freeze_base,
            optimizer=args.optimizer,
            learning_rate=args.learning_rate,
            weight_decay=args.weight_decay,
            lr_scheduler=args.lr_scheduler,
            warmup_steps=args.warmup_steps,
            max_steps=args.max_steps,
            dropout=args.dropout,
            hidden_size=args.hidden_size,
        )


FluorescenceTask = TAPETask(
    "fluorescence", FluorescenceDataModule, FluorescencePredictor  # type: ignore
)
