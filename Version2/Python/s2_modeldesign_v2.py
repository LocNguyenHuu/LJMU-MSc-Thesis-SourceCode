# -*- coding: utf-8 -*-
"""S2-ModelDesign-v2.ipynb

Automatically generated by Colab.

Original file is located at
    https://colab.research.google.com/drive/1YLj6xkWOkjflMts-LQRkusqtyKjEOqMu
"""

# Install Dependencies

!pip uninstall -y numpy
!pip install --force-reinstall numpy==1.26.4

!pip install torch==2.2.1 torchvision==0.17.1 torchaudio==2.2.1 sentence-transformers==2.2.2
!pip install pandas==2.0.0
!pip install transformers==4.41.0 scikit-learn==1.2.0
!pip install huggingface-hub==0.25.2
!pip install nltk==3.8.1 rouge-score==0.1.2 bert-score==0.3.13 -q
!pip install tqdm==4.66.5 -q

# Setup and Imports

import pandas as pd
import numpy as np
import torch
from torch.utils.data import Dataset, DataLoader
from transformers import (
    BartForConditionalGeneration, BartTokenizer,
    DPRContextEncoder, DPRQuestionEncoder,
    DPRContextEncoderTokenizer, DPRQuestionEncoderTokenizer
)
from sentence_transformers import SentenceTransformer
from sklearn.model_selection import train_test_split
import os
from google.colab import drive
from tqdm import tqdm
from nltk.translate.bleu_score import sentence_bleu
from rouge_score import rouge_scorer
from bert_score import score as bert_score
from torch.cuda.amp import GradScaler, autocast
from torch.optim.lr_scheduler import CosineAnnealingLR
import string
import nltk
import json

nltk.download('wordnet')
nltk.download('punkt')

drive.mount('/content/drive')

# Configuration
class Config:
    BASE_PATH = "/content/drive/MyDrive/LJMU-Datasets"
    DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    BART_MODEL_NAME = "facebook/bart-base"
    DPR_CTX_MODEL_NAME = "facebook/dpr-ctx_encoder-single-nq-base"
    DPR_QUESTION_MODEL_NAME = "facebook/dpr-question_encoder-single-nq-base"
    BATCH_SIZE = 8
    MAX_EPOCHS = 3
    NUM_WORKERS = 4
    MAX_LENGTH = 256
    SUBSET_SIZE = 500
    HOTPOTQA_MAX_SAMPLES = 1000
    WIKIDATA_SUBSET_SIZE = 30000

CONFIG = Config()
print(f"Using device: {CONFIG.DEVICE}")

# Clear GPU memory
torch.cuda.empty_cache()

# Data Collection and Preprocessing

# Load datasets
qa_train_path = os.path.join(CONFIG.BASE_PATH, "qa_train_v3.csv")
qa_val_path = os.path.join(CONFIG.BASE_PATH, "qa_val_v3.csv")
triple_train_path = os.path.join(CONFIG.BASE_PATH, "triple_train_v3.csv")

qa_train_df = pd.read_csv(qa_train_path)
qa_val_df = pd.read_csv(qa_val_path)
triple_train_df = pd.read_csv(triple_train_path)

# Balance datasets
min_size = min(len(qa_train_df), len(triple_train_df))
qa_train_df = qa_train_df.sample(n=min_size, random_state=42)
triple_train_df = triple_train_df.sample(n=min_size, random_state=42)

print(f"Balanced datasets: QA Train={len(qa_train_df)}, QA Val={len(qa_val_df)}, Triple Train={len(triple_train_df)}")

# Custom Dataset for BART and DPR
class RetrievalDataset(Dataset):
    def __init__(self, df: pd.DataFrame, bart_tokenizer: BartTokenizer, dpr_question_tokenizer: DPRQuestionEncoderTokenizer,
                 max_length: int = 256, task: str = "qa", candidate_objects: list = None):
        self.bart_tokenizer = bart_tokenizer
        self.dpr_question_tokenizer = dpr_question_tokenizer
        self.max_length = max_length
        self.task = task
        self.data = df
        self.candidate_objects = candidate_objects

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        row = self.data.iloc[idx]
        question = row["question"]
        context = row["context"]
        answer = row["answer"]

        if self.task == "qa":
            bart_input_text = f"question: {question} context: {context}"
        else:
            bart_input_text = f"question: {question} context: {context}"
        bart_inputs = self.bart_tokenizer(
            bart_input_text,
            return_tensors="pt",
            max_length=self.max_length,
            truncation=True,
            padding="max_length"
        )
        bart_labels = self.bart_tokenizer(
            answer,
            return_tensors="pt",
            max_length=self.max_length,
            truncation=True,
            padding="max_length"
        )

        dpr_inputs = self.dpr_question_tokenizer(
            question,
            return_tensors="pt",
            max_length=self.max_length,
            truncation=True,
            padding="max_length"
        )

        item = {
            "task": self.task,
            "bart_input_ids": bart_inputs["input_ids"].squeeze(),
            "bart_attention_mask": bart_inputs["attention_mask"].squeeze(),
            "bart_labels": bart_labels["input_ids"].squeeze(),
            "dpr_input_ids": dpr_inputs["input_ids"].squeeze(),
            "dpr_attention_mask": dpr_inputs["attention_mask"].squeeze(),
            "question": question,
            "answer": answer
        }

        if self.task == "triple" and self.candidate_objects:
            label_idx = self.candidate_objects.index(answer) if answer in self.candidate_objects else -1
            item["label_idx"] = label_idx

        return item

bart_tokenizer = BartTokenizer.from_pretrained(CONFIG.BART_MODEL_NAME)
dpr_question_tokenizer = DPRQuestionEncoderTokenizer.from_pretrained(CONFIG.DPR_QUESTION_MODEL_NAME)

triple_candidates = list(set(triple_train_df["answer"].tolist()))
print(f"Number of unique triple candidates: {len(triple_candidates)}")

qa_train_dataset = RetrievalDataset(qa_train_df, bart_tokenizer, dpr_question_tokenizer, task="qa")
qa_val_dataset = RetrievalDataset(qa_val_df, bart_tokenizer, dpr_question_tokenizer, task="qa")
triple_train_dataset = RetrievalDataset(triple_train_df, bart_tokenizer, dpr_question_tokenizer, task="triple", candidate_objects=triple_candidates)

qa_train_loader = DataLoader(qa_train_dataset, batch_size=CONFIG.BATCH_SIZE, shuffle=True, num_workers=CONFIG.NUM_WORKERS)
qa_val_loader = DataLoader(qa_val_dataset, batch_size=CONFIG.BATCH_SIZE, shuffle=False, num_workers=CONFIG.NUM_WORKERS)
triple_train_loader = DataLoader(triple_train_dataset, batch_size=CONFIG.BATCH_SIZE, shuffle=True, num_workers=CONFIG.NUM_WORKERS)

print(f"Created DataLoaders: QA Train={len(qa_train_dataset)}, QA Val={len(qa_val_dataset)}, Triple Train={len(triple_train_dataset)}")

# Define Custom Loss Functions

# Hard negative selection (for DPR embeddings)
def select_hard_negatives(embeddings, candidate_embeddings, correct_answers, all_candidates, num_negatives: int = 10):
    embeddings = embeddings.cpu().detach().numpy()
    candidate_embeddings = candidate_embeddings.cpu().detach().numpy()
    similarities = np.dot(embeddings, candidate_embeddings.T)
    hard_negative_indices = []
    for i in range(len(embeddings)):
        correct_idx = all_candidates.index(correct_answers[i]) if correct_answers[i] in all_candidates else -1
        sim_scores = similarities[i].copy()
        if correct_idx != -1:
            sim_scores[correct_idx] = -float('inf')
        valid_indices = np.where(sim_scores > 0.8)[0]
        if len(valid_indices) < num_negatives:
            valid_indices = np.argsort(-sim_scores)[:num_negatives]
        else:
            valid_indices = np.argsort(-sim_scores[valid_indices])[:num_negatives]
        hard_negative_indices.append(valid_indices)
    return torch.tensor(hard_negative_indices, device=CONFIG.DEVICE)

# InfoNCE loss for DPR fine-tuning
def info_nce_loss(similarities, labels):
    return torch.nn.functional.cross_entropy(similarities, labels)

# BART generation loss
def bart_generation_loss(outputs, labels, ignore_index: int = bart_tokenizer.pad_token_id):
    logits = outputs.logits
    shift_logits = logits[..., :-1, :].contiguous()
    shift_labels = labels[..., 1:].contiguous()
    loss_fn = torch.nn.CrossEntropyLoss(ignore_index=ignore_index)
    loss = loss_fn(shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1))
    return loss

# Normalize text for evaluation
def normalize_text(text: str) -> str:
    text = str(text).lower().strip()
    text = text.translate(str.maketrans("", "", string.punctuation))
    articles = {'a', 'an', 'the'}
    words = text.split()
    words = [word for word in words if word not in articles]
    return ' '.join(words)

print("Defined custom loss functions for BART and DPR.")

# Fine-Tune BART for QA with A100 Optimizations

import torch.optim as optim

def fine_tune_bart_qa(train_loader, val_loader, epochs: int = 3, checkpoint_path: str = None):
    print("Fine-tuning BART for QA...")

    bart_model = BartForConditionalGeneration.from_pretrained(CONFIG.BART_MODEL_NAME).to(CONFIG.DEVICE)
    optimizer = optim.AdamW(bart_model.parameters(), lr=3e-4)
    total_steps = len(train_loader) * epochs
    warmup_steps = int(total_steps * 0.1)
    scheduler = CosineAnnealingLR(optimizer, T_max=total_steps - warmup_steps, eta_min=1e-6)
    scaler = GradScaler()

    start_epoch = 0
    best_loss = float("inf")
    patience, max_patience = 0, 5

    if checkpoint_path and os.path.exists(checkpoint_path):
        checkpoint = torch.load(checkpoint_path, map_location=CONFIG.DEVICE)
        bart_model.load_state_dict(checkpoint["model_state_dict"])
        optimizer.load_state_dict(checkpoint["optimizer_state_dict"])
        scheduler.load_state_dict(checkpoint["scheduler_state_dict"])
        start_epoch = checkpoint["epoch"] + 1
        best_loss = checkpoint["best_loss"]
        patience = checkpoint["patience"]
        print(f"Resumed training from checkpoint at epoch {start_epoch} with best loss {best_loss:.4f}")

    for epoch in range(start_epoch, epochs):
        bart_model.train()
        total_loss = 0
        for step, batch in enumerate(tqdm(train_loader, desc=f"Epoch {epoch+1}/{epochs}")):
            input_ids = batch["bart_input_ids"].to(CONFIG.DEVICE)
            attention_mask = batch["bart_attention_mask"].to(CONFIG.DEVICE)
            labels = batch["bart_labels"].to(CONFIG.DEVICE)

            optimizer.zero_grad()
            with autocast():
                outputs = bart_model(input_ids=input_ids, attention_mask=attention_mask, labels=labels)
                loss = bart_generation_loss(outputs, labels)
            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()

            total_loss += loss.item()

            if step < warmup_steps:
                lr = (step + 1) / warmup_steps * 3e-4
                for param_group in optimizer.param_groups:
                    param_group['lr'] = lr
            else:
                scheduler.step()

            del input_ids, attention_mask, labels, outputs, loss
            torch.cuda.empty_cache()

        avg_loss = total_loss / len(train_loader)
        print(f"Epoch {epoch+1}/{epochs} - Train Loss: {avg_loss:.4f}, LR: {optimizer.param_groups[0]['lr']}")

        bart_model.eval()
        val_loss = 0
        with torch.no_grad():
            for batch in val_loader:
                input_ids = batch["bart_input_ids"].to(CONFIG.DEVICE)
                attention_mask = batch["bart_attention_mask"].to(CONFIG.DEVICE)
                labels = batch["bart_labels"].to(CONFIG.DEVICE)
                with autocast():
                    outputs = bart_model(input_ids=input_ids, attention_mask=attention_mask, labels=labels)
                    val_loss += bart_generation_loss(outputs, labels).item()
                del input_ids, attention_mask, labels, outputs
                torch.cuda.empty_cache()
        val_loss /= len(val_loader)
        print(f"Epoch {epoch+1}/{epochs} - Val Loss: {val_loss:.4f}")

        with torch.no_grad():
            batch = next(iter(val_loader))
            input_ids = batch["bart_input_ids"][:5].to(CONFIG.DEVICE)
            attention_mask = batch["bart_attention_mask"][:5].to(CONFIG.DEVICE)
            generated_ids = bart_model.generate(
                input_ids=input_ids,
                attention_mask=attention_mask,
                max_new_tokens=100,
                num_beams=20,
                temperature=0.5,
                no_repeat_ngram_size=2
            )
            generated_texts = [bart_tokenizer.decode(g_ids, skip_special_tokens=True).lower().strip() for g_ids in generated_ids]
            for gen, ref in zip(generated_texts, batch["answer"][:5]):
                print(f"Generated: {gen}")
                print(f"Reference: {ref}\n")
            del input_ids, attention_mask, generated_ids
            torch.cuda.empty_cache()

        checkpoint = {
            "epoch": epoch,
            "model_state_dict": bart_model.state_dict(),
            "optimizer_state_dict": optimizer.state_dict(),
            "scheduler_state_dict": scheduler.state_dict(),
            "train_loss": avg_loss,
            "val_loss": val_loss,
            "best_loss": best_loss,
            "patience": patience
        }
        epoch_checkpoint_path = os.path.join(CONFIG.BASE_PATH, f"bart_qa_checkpoint_epoch_{epoch+1}_v3.pt")
        torch.save(checkpoint, epoch_checkpoint_path)
        print(f"Saved checkpoint for epoch {epoch+1} at {epoch_checkpoint_path}")

        if val_loss < best_loss:
            best_loss = val_loss
            patience = 0
            best_checkpoint_path = os.path.join(CONFIG.BASE_PATH, "bart_qa_v3.pt")
            torch.save(checkpoint, best_checkpoint_path)
            print(f"Saved best BART QA model with val loss {best_loss:.4f} at {best_checkpoint_path}")
        else:
            patience += 1
            if patience >= max_patience:
                print("Early stopping triggered.")
                break

    return bart_model

try:
    bart_qa_model = fine_tune_bart_qa(qa_train_loader, qa_val_loader, checkpoint_path=None)
except Exception as e:
    print(f"Error in BART QA fine-tuning: {e}")
    raise

# Fine-Tune BART for Triple Retrieval with A100 Optimizations

def fine_tune_bart_triple(train_loader, val_loader, epochs: int = CONFIG.MAX_EPOCHS, checkpoint_path: str = None):
    print("Fine-tuning BART for triple retrieval...")

    bart_model = BartForConditionalGeneration.from_pretrained(CONFIG.BART_MODEL_NAME).to(CONFIG.DEVICE)
    optimizer = optim.AdamW(bart_model.parameters(), lr=3e-4)
    total_steps = len(train_loader) * epochs
    warmup_steps = int(total_steps * 0.1)
    scheduler = CosineAnnealingLR(optimizer, T_max=total_steps - warmup_steps, eta_min=1e-6)
    scaler = GradScaler()

    start_epoch = 0
    best_loss = float("inf")
    patience, max_patience = 0, 5

    if checkpoint_path and os.path.exists(checkpoint_path):
        checkpoint = torch.load(checkpoint_path, map_location=CONFIG.DEVICE)
        bart_model.load_state_dict(checkpoint["model_state_dict"])
        optimizer.load_state_dict(checkpoint["optimizer_state_dict"])
        scheduler.load_state_dict(checkpoint["scheduler_state_dict"])
        start_epoch = checkpoint["epoch"] + 1
        best_loss = checkpoint["best_loss"]
        patience = checkpoint["patience"]
        print(f"Resumed training from checkpoint at epoch {start_epoch} with best loss {best_loss:.4f}")

    for epoch in range(start_epoch, epochs):
        bart_model.train()
        total_loss = 0
        for step, batch in enumerate(tqdm(train_loader, desc=f"Epoch {epoch+1}/{epochs}")):
            input_ids = batch["bart_input_ids"].to(CONFIG.DEVICE)
            attention_mask = batch["bart_attention_mask"].to(CONFIG.DEVICE)
            labels = batch["bart_labels"].to(CONFIG.DEVICE)

            optimizer.zero_grad()
            with autocast():
                outputs = bart_model(input_ids=input_ids, attention_mask=attention_mask, labels=labels)
                loss = bart_generation_loss(outputs, labels)
            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()

            total_loss += loss.item()

            if step < warmup_steps:
                lr = (step + 1) / warmup_steps * 3e-4
                for param_group in optimizer.param_groups:
                    param_group['lr'] = lr
            else:
                scheduler.step()

            del input_ids, attention_mask, labels, outputs, loss
            torch.cuda.empty_cache()

        avg_loss = total_loss / len(train_loader)
        print(f"Epoch {epoch+1}/{epochs} - Train Loss: {avg_loss:.4f}, LR: {optimizer.param_groups[0]['lr']}")

        bart_model.eval()
        val_loss = 0
        with torch.no_grad():
            for batch in val_loader:
                input_ids = batch["bart_input_ids"].to(CONFIG.DEVICE)
                attention_mask = batch["bart_attention_mask"].to(CONFIG.DEVICE)
                labels = batch["bart_labels"].to(CONFIG.DEVICE)
                with autocast():
                    outputs = bart_model(input_ids=input_ids, attention_mask=attention_mask, labels=labels)
                    val_loss += bart_generation_loss(outputs, labels).item()
                del input_ids, attention_mask, labels, outputs
                torch.cuda.empty_cache()
        val_loss /= len(val_loader)
        print(f"Epoch {epoch+1}/{epochs} - Val Loss: {val_loss:.4f}")

        with torch.no_grad():
            batch = next(iter(val_loader))
            input_ids = batch["bart_input_ids"][:5].to(CONFIG.DEVICE)
            attention_mask = batch["bart_attention_mask"][:5].to(CONFIG.DEVICE)
            generated_ids = bart_model.generate(
                input_ids=input_ids,
                attention_mask=attention_mask,
                max_new_tokens=50,
                num_beams=15,
                temperature=0.5,
                no_repeat_ngram_size=2
            )
            generated_texts = [bart_tokenizer.decode(g_ids, skip_special_tokens=True).lower().strip() for g_ids in generated_ids]
            for gen, ref in zip(generated_texts, batch["answer"][:5]):
                print(f"Generated: {gen}")
                print(f"Reference: {ref}\n")
            del input_ids, attention_mask, generated_ids
            torch.cuda.empty_cache()

        checkpoint = {
            "epoch": epoch,
            "model_state_dict": bart_model.state_dict(),
            "optimizer_state_dict": optimizer.state_dict(),
            "scheduler_state_dict": scheduler.state_dict(),
            "train_loss": avg_loss,
            "val_loss": val_loss,
            "best_loss": best_loss,
            "patience": patience
        }
        epoch_checkpoint_path = os.path.join(CONFIG.BASE_PATH, f"bart_triple_checkpoint_epoch_{epoch+1}_v3.pt")
        torch.save(checkpoint, epoch_checkpoint_path)
        print(f"Saved checkpoint for epoch {epoch+1} at {epoch_checkpoint_path}")

        if val_loss < best_loss:
            best_loss = val_loss
            patience = 0
            best_checkpoint_path = os.path.join(CONFIG.BASE_PATH, "bart_triple_v3.pt")
            torch.save(checkpoint, best_checkpoint_path)
            print(f"Saved best BART Triple model with val loss {best_loss:.4f} at {best_checkpoint_path}")
        else:
            patience += 1
            if patience >= max_patience:
                print("Early stopping triggered.")
                break

    return bart_model

# Create a validation loader for triple data
triple_train_df, triple_val_df = train_test_split(triple_train_df, train_size=0.8, random_state=42)
triple_val_dataset = RetrievalDataset(triple_val_df, bart_tokenizer, dpr_question_tokenizer, task="triple", candidate_objects=triple_candidates)
triple_val_loader = DataLoader(triple_val_dataset, batch_size=CONFIG.BATCH_SIZE, shuffle=False, num_workers=CONFIG.NUM_WORKERS)

try:
    bart_triple_model = fine_tune_bart_triple(triple_train_loader, triple_val_loader, checkpoint_path=None)
except Exception as e:
    print(f"Error in BART triple fine-tuning: {e}")
    raise

# Save Artifacts to Google Drive

import pickle

# Mount Google Drive
drive.mount('/content/drive')

# Define save path
save_path = '/content/drive/MyDrive/bert_retrieval_artifacts_v3'
os.makedirs(save_path, exist_ok=True)

# Load sentence transformer
sentence_transformer = SentenceTransformer('all-MiniLM-L6-v2')

# Save DataLoaders
with open(os.path.join(save_path, 'qa_train_loader_v3.pkl'), 'wb') as f:
    pickle.dump(qa_train_loader, f)
with open(os.path.join(save_path, 'qa_val_loader_v3.pkl'), 'wb') as f:
    pickle.dump(qa_val_loader, f)
with open(os.path.join(save_path, 'triple_train_loader_v3.pkl'), 'wb') as f:
    pickle.dump(triple_train_loader, f)
with open(os.path.join(save_path, 'triple_val_loader_v3.pkl'), 'wb') as f:
    pickle.dump(triple_val_loader, f)

# Save triple_candidates
with open(os.path.join(save_path, 'triple_candidates_v3.pkl'), 'wb') as f:
    pickle.dump(triple_candidates, f)

# Save BART models
torch.save(bart_qa_model.state_dict(), os.path.join(save_path, 'bart_qa_v3.pt'))
torch.save(bart_triple_model.state_dict(), os.path.join(save_path, 'bart_triple_v3.pt'))

# Save sentence_transformer
with open(os.path.join(save_path, 'sentence_transformer_v3.pkl'), 'wb') as f:
    pickle.dump(sentence_transformer, f)

# Compute all_candidates directly from DataLoaders
all_candidates = []
for batch in qa_train_loader:
    all_candidates.extend(batch["answer"])
for batch in qa_val_loader:
    all_candidates.extend(batch["answer"])
all_candidates.extend(triple_candidates)
all_candidates = list(set(all_candidates))[:5000]

# Save all_candidates
with open(os.path.join(save_path, 'all_candidates_v3.pkl'), 'wb') as f:
    pickle.dump(all_candidates, f)

print("Artifacts saved to Google Drive!")

# Fine-Tune DPR for Discriminative Retrieval on QA Task

# Clear GPU memory before starting
import torch
import gc
torch.cuda.empty_cache()
gc.collect()

# Load artifacts
save_path = '/content/drive/MyDrive/bert_retrieval_artifacts_v3'

# Load DataLoaders
with open(os.path.join(save_path, 'qa_train_loader_v3.pkl'), 'rb') as f:
    qa_train_loader = pickle.load(f)
with open(os.path.join(save_path, 'qa_val_loader_v3.pkl'), 'rb') as f:
    qa_val_loader = pickle.load(f)

# Load all_candidates
with open(os.path.join(save_path, 'all_candidates_v3.pkl'), 'rb') as f:
    all_candidates = pickle.load(f)

# Load DPR models and tokenizers
ctx_encoder = DPRContextEncoder.from_pretrained(CONFIG.DPR_CTX_MODEL_NAME).to(CONFIG.DEVICE)
question_encoder = DPRQuestionEncoder.from_pretrained(CONFIG.DPR_QUESTION_MODEL_NAME).to(CONFIG.DEVICE)
ctx_tokenizer = DPRContextEncoderTokenizer.from_pretrained(CONFIG.DPR_CTX_MODEL_NAME)
question_tokenizer = DPRQuestionEncoderTokenizer.from_pretrained(CONFIG.DPR_QUESTION_MODEL_NAME)

# Encode all candidates
print("Encoding candidates...")
candidate_inputs = ctx_tokenizer(all_candidates, return_tensors="pt", padding=True, truncation=True, max_length=CONFIG.MAX_LENGTH)
candidate_inputs = {k: v.to(CONFIG.DEVICE) for k, v in candidate_inputs.items()}
with torch.no_grad():
    candidate_embeddings = ctx_encoder(**candidate_inputs).pooler_output
torch.save(candidate_embeddings, os.path.join(save_path, 'dpr_candidate_embeddings_v3.pt'))

# Fine-tune DPR
optimizer = torch.optim.AdamW(list(ctx_encoder.parameters()) + list(question_encoder.parameters()), lr=2e-5)
scaler = torch.cuda.amp.GradScaler()
epochs = CONFIG.MAX_EPOCHS

for epoch in range(epochs):
    ctx_encoder.train()
    question_encoder.train()
    total_loss = 0
    for batch in tqdm(qa_train_loader, desc=f"Epoch {epoch+1}/{epochs}"):
        question_inputs = {
            "input_ids": batch["dpr_input_ids"].to(CONFIG.DEVICE),
            "attention_mask": batch["dpr_attention_mask"].to(CONFIG.DEVICE)
        }
        correct_answers = batch["answer"]
        optimizer.zero_grad(set_to_none=True)
        with torch.cuda.amp.autocast():
            question_embeddings = question_encoder(**question_inputs).pooler_output  # Shape: (batch_size, 768)
            similarities = torch.matmul(question_embeddings, candidate_embeddings.T)  # Shape: (batch_size, num_candidates)
            batch_size = question_embeddings.size(0)
            labels = torch.zeros(batch_size, dtype=torch.long, device=CONFIG.DEVICE)
            for i in range(batch_size):
                correct_answer = correct_answers[i]
                if correct_answer in all_candidates:
                    labels[i] = all_candidates.index(correct_answer)
                else:
                    labels[i] = 0
            loss = torch.nn.functional.cross_entropy(similarities, labels)
        scaler.scale(loss).backward()
        scaler.step(optimizer)
        scaler.update()
        total_loss += loss.item()
        del question_inputs, similarities, labels, loss
        torch.cuda.empty_cache()
    avg_loss = total_loss / len(qa_train_loader)
    print(f"Epoch {epoch+1}/{epochs} - Train Loss: {avg_loss:.4f}")

# Evaluate DPR
def evaluate_dpr(ctx_encoder, question_encoder, val_loader, candidates, small_candidate_pool: bool = False):
    ctx_encoder.eval()
    question_encoder.eval()
    mrr, precision_at_1 = [], []
    eval_candidates = candidates[:100] if small_candidate_pool else candidates
    print(f"Using candidate pool size: {len(eval_candidates)}")
    candidate_inputs = ctx_tokenizer(eval_candidates, return_tensors="pt", padding=True, truncation=True, max_length=CONFIG.MAX_LENGTH)
    candidate_inputs = {k: v.to(CONFIG.DEVICE) for k, v in candidate_inputs.items()}
    with torch.no_grad():
        candidate_embeddings = ctx_encoder(**candidate_inputs).pooler_output
    with torch.no_grad():
        for batch in tqdm(val_loader, desc="Evaluating"):
            question_inputs = {
                "input_ids": batch["dpr_input_ids"].to(CONFIG.DEVICE),
                "attention_mask": batch["dpr_attention_mask"].to(CONFIG.DEVICE)
            }
            references = batch["answer"]
            question_embeddings = question_encoder(**question_inputs).pooler_output
            similarities = torch.matmul(question_embeddings, candidate_embeddings.T)
            rankings = torch.argsort(similarities, dim=1, descending=True)
            for i, (ranking, ref) in enumerate(zip(rankings, references)):
                ref_idx = eval_candidates.index(ref) if ref in eval_candidates else -1
                if ref_idx == -1:
                    continue
                rank = (ranking == ref_idx).nonzero(as_tuple=True)[0].item() + 1 if ref_idx in ranking else len(eval_candidates)
                mrr.append(1.0 / rank)
                precision_at_1.append(1.0 if rank == 1 else 0.0)
            del question_inputs, similarities, rankings
            torch.cuda.empty_cache()
    avg_mrr = np.mean(mrr)
    avg_precision_at_1 = np.mean(precision_at_1)
    print("DPR Evaluation:")
    print(f"MRR: {avg_mrr:.4f}")
    print(f"Precision@1: {avg_precision_at_1:.4f}")
    return avg_mrr, avg_precision_at_1

# Evaluate DPR on full and small candidate pools
dpr_mrr_full_qa, dpr_precision_full_qa = evaluate_dpr(ctx_encoder, question_encoder, qa_val_loader, all_candidates, small_candidate_pool=False)
dpr_mrr_small_qa, dpr_precision_small_qa = evaluate_dpr(ctx_encoder, question_encoder, qa_val_loader, all_candidates, small_candidate_pool=True)

# Save DPR models
ctx_encoder.save_pretrained(os.path.join(CONFIG.BASE_PATH, "dpr_ctx_encoder_qa_v3"))
question_encoder.save_pretrained(os.path.join(CONFIG.BASE_PATH, "dpr_question_encoder_qa_v3"))
print("Saved DPR models for QA task.")

# Fine-Tune DPR for Discriminative Retrieval on Triple Task

# Clear GPU memory before starting
torch.cuda.empty_cache()
gc.collect()

# Load DataLoaders
with open(os.path.join(save_path, 'triple_train_loader_v3.pkl'), 'rb') as f:
    triple_train_loader = pickle.load(f)
with open(os.path.join(save_path, 'triple_val_loader_v3.pkl'), 'rb') as f:
    triple_val_loader = pickle.load(f)

# Load all_candidates
with open(os.path.join(save_path, 'all_candidates_v3.pkl'), 'rb') as f:
    all_candidates = pickle.load(f)

# Load DPR models and tokenizers (start fresh to avoid overfitting from QA fine-tuning)
ctx_encoder = DPRContextEncoder.from_pretrained(CONFIG.DPR_CTX_MODEL_NAME).to(CONFIG.DEVICE)
question_encoder = DPRQuestionEncoder.from_pretrained(CONFIG.DPR_QUESTION_MODEL_NAME).to(CONFIG.DEVICE)
ctx_tokenizer = DPRContextEncoderTokenizer.from_pretrained(CONFIG.DPR_CTX_MODEL_NAME)
question_tokenizer = DPRQuestionEncoderTokenizer.from_pretrained(CONFIG.DPR_QUESTION_MODEL_NAME)

# Encode all candidates
print("Encoding candidates for triple task...")
candidate_inputs = ctx_tokenizer(all_candidates, return_tensors="pt", padding=True, truncation=True, max_length=CONFIG.MAX_LENGTH)
candidate_inputs = {k: v.to(CONFIG.DEVICE) for k, v in candidate_inputs.items()}
with torch.no_grad():
    candidate_embeddings = ctx_encoder(**candidate_inputs).pooler_output
torch.save(candidate_embeddings, os.path.join(save_path, 'dpr_candidate_embeddings_triple_v3.pt'))

# Fine-tune DPR on triple task
optimizer = torch.optim.AdamW(list(ctx_encoder.parameters()) + list(question_encoder.parameters()), lr=2e-5)
scaler = torch.cuda.amp.GradScaler()
epochs = CONFIG.MAX_EPOCHS

for epoch in range(epochs):
    ctx_encoder.train()
    question_encoder.train()
    total_loss = 0
    for batch in tqdm(triple_train_loader, desc=f"Epoch {epoch+1}/{epochs}"):
        question_inputs = {
            "input_ids": batch["dpr_input_ids"].to(CONFIG.DEVICE),
            "attention_mask": batch["dpr_attention_mask"].to(CONFIG.DEVICE)
        }
        correct_answers = batch["answer"]
        optimizer.zero_grad(set_to_none=True)
        with torch.cuda.amp.autocast():
            question_embeddings = question_encoder(**question_inputs).pooler_output  # Shape: (batch_size, 768)
            similarities = torch.matmul(question_embeddings, candidate_embeddings.T)  # Shape: (batch_size, num_candidates)
            batch_size = question_embeddings.size(0)
            labels = torch.zeros(batch_size, dtype=torch.long, device=CONFIG.DEVICE)
            for i in range(batch_size):
                correct_answer = correct_answers[i]
                if correct_answer in all_candidates:
                    labels[i] = all_candidates.index(correct_answer)
                else:
                    labels[i] = 0
            loss = torch.nn.functional.cross_entropy(similarities, labels)
        scaler.scale(loss).backward()
        scaler.step(optimizer)
        scaler.update()
        total_loss += loss.item()
        del question_inputs, similarities, labels, loss
        torch.cuda.empty_cache()
    avg_loss = total_loss / len(triple_train_loader)
    print(f"Epoch {epoch+1}/{epochs} - Train Loss: {avg_loss:.4f}")

# Evaluate DPR on triple task
dpr_mrr_full_triple, dpr_precision_full_triple = evaluate_dpr(ctx_encoder, question_encoder, triple_val_loader, all_candidates, small_candidate_pool=False)
dpr_mrr_small_triple, dpr_precision_small_triple = evaluate_dpr(ctx_encoder, question_encoder, triple_val_loader, all_candidates, small_candidate_pool=True)

# Save DPR models for triple task
ctx_encoder.save_pretrained(os.path.join(CONFIG.BASE_PATH, "dpr_ctx_encoder_triple_v3"))
question_encoder.save_pretrained(os.path.join(CONFIG.BASE_PATH, "dpr_question_encoder_triple_v3"))
print("Saved DPR models for triple task.")

# Redesign and Evaluate Ensemble with DPR for QA and Triple Tasks

# Clear GPU memory before starting
torch.cuda.empty_cache()
gc.collect()

# Load artifacts
save_path = '/content/drive/MyDrive/bert_retrieval_artifacts_v3'

# Load DataLoaders
with open(os.path.join(save_path, 'qa_val_loader_v3.pkl'), 'rb') as f:
    qa_val_loader = pickle.load(f)
with open(os.path.join(save_path, 'triple_val_loader_v3.pkl'), 'rb') as f:
    triple_val_loader = pickle.load(f)

# Load all_candidates
with open(os.path.join(save_path, 'all_candidates_v3.pkl'), 'rb') as f:
    all_candidates = pickle.load(f)

# Load DPR models and tokenizers for QA task
ctx_encoder_qa = DPRContextEncoder.from_pretrained(os.path.join(CONFIG.BASE_PATH, "dpr_ctx_encoder_qa_v3")).to(CONFIG.DEVICE)
question_encoder_qa = DPRQuestionEncoder.from_pretrained(os.path.join(CONFIG.BASE_PATH, "dpr_question_encoder_qa_v3")).to(CONFIG.DEVICE)
ctx_tokenizer = DPRContextEncoderTokenizer.from_pretrained(CONFIG.DPR_CTX_MODEL_NAME)
question_tokenizer = DPRQuestionEncoderTokenizer.from_pretrained(CONFIG.DPR_QUESTION_MODEL_NAME)
candidate_embeddings_qa = torch.load(os.path.join(save_path, 'dpr_candidate_embeddings_v3.pt')).to(CONFIG.DEVICE)

# Load DPR models and tokenizers for triple task
ctx_encoder_triple = DPRContextEncoder.from_pretrained(os.path.join(CONFIG.BASE_PATH, "dpr_ctx_encoder_triple_v3")).to(CONFIG.DEVICE)
question_encoder_triple = DPRQuestionEncoder.from_pretrained(os.path.join(CONFIG.BASE_PATH, "dpr_question_encoder_triple_v3")).to(CONFIG.DEVICE)
candidate_embeddings_triple = torch.load(os.path.join(save_path, 'dpr_candidate_embeddings_triple_v3.pt')).to(CONFIG.DEVICE)

# Evaluate BART on QA and triple tasks
def evaluate_bart(model, val_loader, task: str = "qa"):
    print(f"Evaluating BART for {task}...")
    model.eval()
    bleu_scores, rouge_scores, bert_scores = [], [], []
    sample_outputs = []
    scorer = rouge_scorer.RougeScorer(['rougeL'], use_stemmer=True)

    with torch.no_grad():
        for batch in tqdm(val_loader, desc="Evaluating"):
            input_ids = batch["bart_input_ids"].to(CONFIG.DEVICE)
            attention_mask = batch["bart_attention_mask"].to(CONFIG.DEVICE)
            references = batch["answer"]
            generated_ids = model.generate(
                input_ids=input_ids,
                attention_mask=attention_mask,
                max_new_tokens=100,
                num_beams=20,
                temperature=0.5,
                no_repeat_ngram_size=2
            )
            generated_texts = [bart_tokenizer.decode(g_ids, skip_special_tokens=True) for g_ids in generated_ids]
            for gen, ref in zip(generated_texts, references):
                gen = normalize_text(gen)
                ref = normalize_text(ref)
                bleu = sentence_bleu([ref.split()], gen.split())
                rouge = scorer.score(ref, gen)['rougeL'].fmeasure
                bert_f1 = bert_score([gen], [ref], lang="en", verbose=False)[2].mean().item()  # Fixed bert_score call
                bleu_scores.append(bleu)
                rouge_scores.append(rouge)
                bert_scores.append(bert_f1)
                sample_outputs.append((gen, ref))
            del input_ids, attention_mask, generated_ids
            torch.cuda.empty_cache()
    avg_bleu = np.mean(bleu_scores)
    avg_rouge = np.mean(rouge_scores)
    avg_bert = np.mean(bert_scores)
    print(f"BART {task} Evaluation:")
    print(f"Average BLEU: {avg_bleu:.4f}")
    print(f"Average ROUGE-L: {avg_rouge:.4f}")
    print(f"Average BERTScore F1: {avg_bert:.4f}")
    print(f"Sample Outputs (First 5) for {task}:")
    for gen, ref in sample_outputs[:5]:
        print(f"Generated: {gen}")
        print(f"Reference: {ref}\n")
    return avg_bleu, avg_rouge, avg_bert

# Evaluate BART on QA and triple tasks
bart_qa_bleu, bart_qa_rouge, bart_qa_bert = evaluate_bart(bart_qa_model, qa_val_loader, task="qa")
bart_triple_bleu, bart_triple_rouge, bart_triple_bert = evaluate_bart(bart_triple_model, triple_val_loader, task="triple")

# Redesign Ensemble: Use DPR to generate candidates, then re-rank
def ensemble_evaluate_dpr(ctx_encoder, question_encoder, val_loader, candidates, top_k: int = 30):
    print("Evaluating DPR-based ensemble (DPR for candidate selection)...")
    ctx_encoder.eval()
    question_encoder.eval()
    mrr, precision_at_1 = [], []
    candidate_inputs = ctx_tokenizer(candidates, return_tensors="pt", padding=True, truncation=True, max_length=CONFIG.MAX_LENGTH)
    candidate_inputs = {k: v.to(CONFIG.DEVICE) for k, v in candidate_inputs.items()}
    with torch.no_grad():
        candidate_embeddings = ctx_encoder(**candidate_inputs).pooler_output
    with torch.no_grad():
        for batch in tqdm(val_loader, desc="Ensemble Evaluating"):
            question_inputs = {
                "input_ids": batch["dpr_input_ids"].to(CONFIG.DEVICE),
                "attention_mask": batch["dpr_attention_mask"].to(CONFIG.DEVICE)
            }
            references = batch["answer"]
            question_embeddings = question_encoder(**question_inputs).pooler_output
            similarities = torch.matmul(question_embeddings, candidate_embeddings.T)
            rankings = torch.argsort(similarities, dim=1, descending=True)
            top_k_indices = rankings[:, :top_k]  # Shape: (batch_size, top_k)
            batch_size = top_k_indices.size(0)
            for i in range(batch_size):
                top_k_candidate_indices = top_k_indices[i].cpu().numpy()
                top_k_candidates = [candidates[idx] for idx in top_k_candidate_indices]
                ref = references[i]
                ref_idx = top_k_candidates.index(ref) if ref in top_k_candidates else -1
                if ref_idx == -1:
                    continue
                rank = (top_k_indices[i] == top_k_candidate_indices[ref_idx]).nonzero(as_tuple=True)[0].item() + 1 if ref_idx >= 0 else len(top_k_candidates)
                mrr.append(1.0 / rank)
                precision_at_1.append(1.0 if rank == 1 else 0.0)
            del question_inputs, similarities, rankings, top_k_indices
            torch.cuda.empty_cache()
    avg_mrr = np.mean(mrr) if mrr else 0.0
    avg_precision_at_1 = np.mean(precision_at_1) if precision_at_1 else 0.0
    print("DPR-based Ensemble Evaluation:")
    print(f"MRR: {avg_mrr:.4f}")
    print(f"Precision@1: {avg_precision_at_1:.4f}")
    return avg_mrr, avg_precision_at_1

# Evaluate ensemble for QA and triple tasks
ensemble_mrr_qa, ensemble_precision_qa = ensemble_evaluate_dpr(ctx_encoder_qa, question_encoder_qa, qa_val_loader, all_candidates)
ensemble_mrr_triple, ensemble_precision_triple = ensemble_evaluate_dpr(ctx_encoder_triple, question_encoder_triple, triple_val_loader, all_candidates)

# Save evaluation results
results = {
    "bart_qa": {"bleu": bart_qa_bleu, "rouge": bart_qa_rouge, "bertscore": bart_qa_bert},
    "bart_triple": {"bleu": bart_triple_bleu, "rouge": bart_triple_rouge, "bertscore": bart_triple_bert},
    "dpr_full_qa": {"mrr": dpr_mrr_full_qa, "precision_at_1": dpr_precision_full_qa},
    "dpr_small_pool_qa": {"mrr": dpr_mrr_small_qa, "precision_at_1": dpr_precision_small_qa},
    "dpr_full_triple": {"mrr": dpr_mrr_full_triple, "precision_at_1": dpr_precision_full_triple},
    "dpr_small_pool_triple": {"mrr": dpr_mrr_small_triple, "precision_at_1": dpr_precision_small_triple},
    "ensemble_qa": {"mrr": ensemble_mrr_qa, "precision_at_1": ensemble_precision_qa},
    "ensemble_triple": {"mrr": ensemble_mrr_triple, "precision_at_1": ensemble_precision_triple}
}

results_path = os.path.join(CONFIG.BASE_PATH, "step2_metrics_v3.json")
with open(results_path, "w") as f:
    json.dump(results, f)
print(f"Saved evaluation results at {results_path}")

