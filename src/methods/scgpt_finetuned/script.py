import copy
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile
import tarfile

import anndata as ad
import gdown
import numpy as np
import scgpt
import torch
from sklearn.model_selection import train_test_split

## VIASH START
# Note: this section is auto-generated by viash at runtime. To edit it, make changes
# in config.vsh.yaml and then run `viash config inject config.vsh.yaml`.
par = {
    "input": "resources_test/.../input.h5ad",
    "output": "output.h5ad",
    "model_name": "scGPT_human",
    "model": "scGPT_human",
    "n_hvg": 3000,
}
meta = {"name": "scgpt"}
## VIASH END

sys.path.append(meta["resources_dir"])
from read_anndata_partial import read_anndata
from exit_codes import exit_non_applicable
from scgpt_functions import evaluate, prepare_data, prepare_dataloader, train

print(f"====== scGPT version {scgpt.__version__} ======", flush=True)

print("\n>>> Reading input files...", flush=True)
print(f"Input H5AD file: '{par['input']}'", flush=True)
adata = read_anndata(par["input"], X="layers/counts", obs="obs", var="var", uns="uns")

if adata.uns["dataset_organism"] != "homo_sapiens":
    exit_non_applicable(
        f"scGPT can only be used with human data "
        f"(dataset_organism == \"{adata.uns['dataset_organism']}\")"
    )

adata.obs["str_batch"] = adata.obs["batch"].astype(str)
adata.obs["batch_id"] = adata.obs["str_batch"].astype("category").cat.codes.values
adata.var["feature_id"] = adata.var_names
adata.var_names = adata.var["feature_name"]

print(adata, flush=True)

if par["model"] is None:
    print(f"\n>>> Downloading '{par['model_name']}' model...", flush=True)
    model_drive_ids = {
        "scGPT_human": "1oWh_-ZRdhtoGQ2Fw24HP41FgLoomVo-y",
        "scGPT_CP": "1_GROJTzXiAV8HB4imruOTk6PEGuNOcgB",
    }
    drive_path = (
        f"https://drive.google.com/drive/folders/{model_drive_ids[par['model_name']]}"
    )
    model_temp = tempfile.TemporaryDirectory()
    model_dir = model_temp.name
    print(f"Downloading from '{drive_path}'", flush=True)
    gdown.download_folder(drive_path, output=model_dir, quiet=True)
else:
    if os.path.isdir(par["model"]):
        print(f"\n>>> Using model directory...", flush=True)
        model_temp = None
        model_dir = par["model"]
    else:
        model_temp = tempfile.TemporaryDirectory()
        model_dir = model_temp.name

        if zipfile.is_zipfile(par["model"]):
            print(f"\n>>> Extracting model from .zip...", flush=True)
            print(f".zip path: '{par['model']}'", flush=True)
            with zipfile.ZipFile(par["model"], "r") as zip_file:
                zip_file.extractall(model_dir)
        elif tarfile.is_tarfile(par["model"]) and par["model"].endswith(
            ".tar.gz"
        ):
            print(f"\n>>> Extracting model from .tar.gz...", flush=True)
            print(f".tar.gz path: '{par['model']}'", flush=True)
            with tarfile.open(par["model"], "r:gz") as tar_file:
                tar_file.extractall(model_dir)
                model_dir = os.path.join(model_dir, os.listdir(model_dir)[0])
        else:
            raise ValueError(
                f"The 'model' argument should be a directory a .zip file or a .tar.gz file"
            )

model_config_file = f"{model_dir}/args.json"
model_file = f"{model_dir}/best_model.pt"
vocab_file = f"{model_dir}/vocab.json"
print(f"Model directory: '{model_dir}'", flush=True)
print(f"Model config file: '{model_config_file}'", flush=True)
print(f"Model file: '{model_file}'", flush=True)
print(f"Model vocabulary file: '{vocab_file}'", flush=True)

print("\n>>> Loading model configuration...", flush=True)
model_settings = {
    # Input and preprocessing
    "pad_token": "<pad>",
    "special_tokens": ["<pad>", "<cls>", "<eoc>"],
    "mask_value": -1,
    "pad_value": -2,
    "n_input_bins": 51,
    # Other settings
    "n_hvg": par["n_hvg"],
    "max_seq_len": par["n_hvg"] + 1,
    "per_seq_batch_sample": True,
    "DSBN": True,
    "explicit_zero_prob": True,
}
print("Model settings:", flush=True)
for key, value in model_settings.items():
    print(f"\t{key}: {value}", flush=True)
vocab = scgpt.tokenizer.gene_tokenizer.GeneVocab.from_file(vocab_file)
for token in model_settings["special_tokens"]:
    if token not in vocab:
        vocab.add_token(token)
adata.var["id_in_vocab"] = [1 if gene in vocab else -1 for gene in adata.var_names]
gene_ids_in_vocab = np.array(adata.var["id_in_vocab"])
scgpt.logger.info(
    f"Matched {np.sum(gene_ids_in_vocab >= 0)}/{len(gene_ids_in_vocab)} genes in vocabulary of {len(vocab)}",
)
adata = adata[:, adata.var["id_in_vocab"] >= 0].copy()
with open(model_config_file, "r") as f:
    pretrained_config = json.load(f)

model_config = {
    "embsize": pretrained_config["embsize"],
    "nheads": pretrained_config["nheads"],
    "d_hid": pretrained_config["d_hid"],
    "nlayers": pretrained_config["nlayers"],
    "n_layers_cls": pretrained_config["n_layers_cls"],
}
print("Model configuration:", flush=True)
for key, value in model_config.items():
    print(f"\t{key}: {value}", flush=True)

print("\n>>> Preprocessing data...", flush=True)
preprocessor = scgpt.preprocess.Preprocessor(
    use_key="X",  # The key in adata.layers to use as raw data
    filter_gene_by_counts=3,  # Number of counts for filtering genes
    filter_cell_by_counts=False,  # Number of counts for filtering cells
    normalize_total=1e4,  # Whether to normalize the raw data and to what sum
    result_normed_key="X_normed",  # The key in adata.layers to store the normalized data
    log1p=True,  # Whether to log1p the normalized data
    result_log1p_key="X_log1p",  # The key in adata.layers to store the log1p data
    subset_hvg=model_settings[
        "n_hvg"
    ],  # Whether to subset the raw data to highly variable genes and to what number
    hvg_flavor="seurat_v3",  # The flavor of highly variable gene selection
    binning=model_settings[
        "n_input_bins"
    ],  # Whether to bin the raw data and to what number of bins
    result_binned_key="X_binned",  # The key in adata.layers to store the binned data
)
preprocessor(adata, batch_key="str_batch")
print(adata, flush=True)

print("\n>>> Splitting and tokenizing data...", flush=True)
celltype_labels = np.array(adata.obs["cell_type"].to_list())
(
    train_data,
    valid_data,
    train_celltype_labels,
    valid_celltype_labels,
    train_batch_labels,
    valid_batch_labels,
) = train_test_split(
    adata.X.A,
    celltype_labels,
    np.array(adata.obs["batch_id"].tolist()),
    test_size=0.1,
    shuffle=True,
)

vocab.set_default_index(vocab["<pad>"])
gene_ids = np.array(vocab(adata.var_names.tolist()), dtype=int)
tokenized_train = scgpt.tokenizer.tokenize_and_pad_batch(
    train_data,
    gene_ids,
    max_len=model_settings["max_seq_len"],
    vocab=vocab,
    pad_token=model_settings["pad_token"],
    pad_value=model_settings["pad_value"],
    append_cls=True,  # Append <cls> token at the beginning
    include_zero_gene=True,
)
scgpt.logger.info(
    f"Number of training samples: {tokenized_train['genes'].shape[0]}, "
    f"\n\tFeature length: {tokenized_train['genes'].shape[1]}"
)
tokenized_valid = scgpt.tokenizer.tokenize_and_pad_batch(
    valid_data,
    gene_ids,
    max_len=model_settings["max_seq_len"],
    vocab=vocab,
    pad_token=model_settings["pad_token"],
    pad_value=model_settings["pad_value"],
    append_cls=True,  # Append <cls> token at the beginning
    include_zero_gene=True,
)
scgpt.logger.info(
    f"Number of validation samples: {tokenized_valid['genes'].shape[0]}, "
    f"\n\tFeature length: {tokenized_valid['genes'].shape[1]}"
)

print("\n>>> Loading pre-trained model...", flush=True)
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print(f"Using '{device}' device")

hyperparameters = {
    "n_tokens": len(vocab),
    "GEPC": True,  # Gene expression modelling for cell objective
    "ecs_thres": 0.8,  # Elastic cell similarity objective, 0.0 to 1.0, 0.0 to disable
    "dab_weight": 1.0,  # DAR objective weight for batch correction
    "mask_ratio": 0.4,
    "epochs": 15,
    "lr": 1e-4,
    "batch_size": 64,
    "dropout": 0.2,
    "schedule_ratio": 0.9,  # Learning rate decay
    "log_interval": 100,
    "fast_transformer": False,  # TODO: Set True if flash-attn is installed
    "pre_norm": False,
    "amp": True,  # Automatic Mixed Precision
}
print("Hyperparameters:", flush=True)
for key, value in hyperparameters.items():
    print(f"\t{key}: {value}", flush=True)
model = scgpt.model.TransformerModel(
    hyperparameters["n_tokens"],
    model_config["embsize"],
    model_config["nheads"],
    model_config["d_hid"],
    model_config["nlayers"],
    vocab=vocab,
    dropout=hyperparameters["dropout"],
    pad_token=model_settings["pad_token"],
    pad_value=model_settings["pad_value"],
    do_mvc=hyperparameters["GEPC"],
    do_dab=True,
    use_batch_labels=True,
    num_batch_labels=len(set(adata.obs["batch_id"].tolist())),
    domain_spec_batchnorm=model_settings["DSBN"],
    n_input_bins=model_settings["n_input_bins"],
    ecs_threshold=hyperparameters["ecs_thres"],
    explicit_zero_prob=model_settings["explicit_zero_prob"],
    use_fast_transformer=hyperparameters["fast_transformer"],
    pre_norm=hyperparameters["pre_norm"],
)
scgpt.utils.load_pretrained(
    model, torch.load(model_file, map_location=torch.device(device)), verbose=False
)
model.to(device)

print("\n>>> Fine-tuning model...", flush=True)
criterion = scgpt.loss.masked_mse_loss
criterion_dab = torch.nn.CrossEntropyLoss()
optimizer = torch.optim.Adam(
    model.parameters(),
    lr=hyperparameters["lr"],
    eps=1e-4 if hyperparameters["amp"] else 1e-8,
)
scheduler = torch.optim.lr_scheduler.StepLR(
    optimizer, 1, gamma=hyperparameters["schedule_ratio"]
)
scaler = torch.cuda.amp.GradScaler(enabled=hyperparameters["amp"])

best_val_loss = float("inf")
best_avg_bio = 0.0
best_model = None

for epoch in range(1, hyperparameters["epochs"] + 1):
    epoch_start_time = time.time()
    train_data_pt, valid_data_pt = prepare_data(
        tokenized_train,
        tokenized_valid,
        train_batch_labels,
        valid_batch_labels,
        hyperparameters,
        model_settings,
        epoch,
    )

    train_loader = prepare_dataloader(
        train_data_pt,
        batch_size=hyperparameters["batch_size"],
        shuffle=False,
        intra_domain_shuffle=True,
        drop_last=False,
        num_workers=0,
        per_seq_batch_sample=model_settings["per_seq_batch_sample"],
    )

    valid_loader = prepare_dataloader(
        valid_data_pt,
        batch_size=hyperparameters["batch_size"],
        shuffle=False,
        intra_domain_shuffle=False,
        drop_last=False,
        num_workers=0,
        per_seq_batch_sample=model_settings["per_seq_batch_sample"],
    )

    train(
        model,
        train_loader,
        scaler,
        optimizer,
        scheduler,
        vocab,
        criterion,
        criterion_dab,
        hyperparameters,
        model_settings,
        device,
        epoch,
    )

    val_loss, val_mre = evaluate(
        model,
        valid_loader,
        vocab,
        criterion,
        criterion_dab,
        hyperparameters,
        model_settings,
        device,
    )
    elapsed = time.time() - epoch_start_time
    scgpt.logger.info("-" * 89)
    scgpt.logger.info(
        f"| end of epoch {epoch:3d} | time: {elapsed:5.2f}s | "
        f"valid loss/mse {val_loss:5.4f} | mre {val_mre:5.4f}"
    )
    scgpt.logger.info("-" * 89)

    if val_loss < best_val_loss:
        best_val_loss = val_loss
        best_model = copy.deepcopy(model)
        best_model_epoch = epoch
        scgpt.logger.info(f"Best model with score {best_val_loss:5.4f}")

    scheduler.step()

print(f"Best model: Epoch {best_model_epoch}, Val loss {best_val_loss}")

print("\n>>> Saving best model...", flush=True)
best_model_dir = tempfile.TemporaryDirectory()
shutil.copy(vocab_file, best_model_dir.name)
shutil.copy(model_config_file, best_model_dir.name)
torch.save(best_model.state_dict(), os.path.join(best_model_dir.name, "best_model.pt"))
print(f"Best model directory: '{best_model_dir.name}'", flush=True)

print("\n>>> Embedding data...", flush=True)
embedded = scgpt.tasks.embed_data(
    adata,
    best_model_dir.name,
    gene_col="feature_name",
    batch_size=64,
    use_fast_transformer=False,  # Disable fast-attn as not installed
    device=device,
    return_new_adata=True,
)

print("\n>>> Storing output...", flush=True)
output = ad.AnnData(
    obs=adata.obs[[]],
    var=adata.var[[]],
    obsm={
        "X_emb": embedded.X,
    },
    uns={
        "dataset_id": adata.uns["dataset_id"],
        "normalization_id": adata.uns["normalization_id"],
        "method_id": meta["name"],
    },
)
print(output)

print("\n>>> Writing output to file...", flush=True)
print(f"Output H5AD file: '{par['output']}'", flush=True)
output.write_h5ad(par["output"], compression="gzip")

print("\n>>> Cleaning up temporary directories...", flush=True)
if model_temp is not None:
    model_temp.cleanup()
best_model_dir.cleanup()

print("\n>>> Done!", flush=True)
