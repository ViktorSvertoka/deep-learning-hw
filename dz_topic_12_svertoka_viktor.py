# -*- coding: utf-8 -*-
"""dz_topic_12_svertoka_viktor.ipynb

Automatically generated by Colab.

Original file is located at
    https://colab.research.google.com/drive/1ExvrXQ70Q89eYgiw_7BWLhb8AlmRGKQc
"""

import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader, Dataset
import pandas as pd
import spacy
import random
import matplotlib.pyplot as plt
import numpy as np
from collections import Counter
from torch.nn.utils.rnn import pad_sequence
import gc

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# 1. Зчитуємо обидва parquet файли та об’єднуємо
df1 = pd.read_parquet("train-00000-of-00002.parquet")
df2 = pd.read_parquet("train-00001-of-00002.parquet")
df = pd.concat([df1, df2], ignore_index=True)

print(df.columns)  # перевір, які назви колонок з текстом

SRC_LANG = "da"
TGT_LANG = "en"

# 2. Завантажуємо spaCy моделі для датської та англійської (якщо ще не зроблено, запусти в Colab):
# !python -m spacy download da_core_news_sm
# !python -m spacy download en_core_web_sm

spacy_src = spacy.load("da_core_news_sm")
spacy_tgt = spacy.load("en_core_web_sm")

def tokenize_src(text):
    return [tok.text.lower() for tok in spacy_src.tokenizer(str(text))]

def tokenize_tgt(text):
    return [tok.text.lower() for tok in spacy_tgt.tokenizer(str(text))]

# 3. Розділяємо на train і val, беремо скорочену кількість речень для економії пам'яті
train_df = df.sample(frac=0.8, random_state=42)
val_df = df.drop(train_df.index)

# Скорочуємо датасет для уникнення Out of Memory
MAX_TRAIN = 5000
MAX_VAL = 1000

train_src_sentences = [tokenize_src(s[SRC_LANG]) for s in train_df['translation'].tolist()[:MAX_TRAIN]]
train_tgt_sentences = [tokenize_tgt(s[TGT_LANG]) for s in train_df['translation'].tolist()[:MAX_TRAIN]]

val_src_sentences = [tokenize_src(s[SRC_LANG]) for s in val_df['translation'].tolist()[:MAX_VAL]]
val_tgt_sentences = [tokenize_tgt(s[TGT_LANG]) for s in val_df['translation'].tolist()[:MAX_VAL]]

def build_vocab(sentences, min_freq=2):
    counter = Counter()
    for sent in sentences:
        counter.update(sent)
    vocab = {"<pad>":0, "<sos>":1, "<eos>":2, "<unk>":3}
    for token, freq in counter.items():
        if freq >= min_freq:
            vocab[token] = len(vocab)
    return vocab

src_vocab = build_vocab(train_src_sentences, min_freq=1)
tgt_vocab = build_vocab(train_tgt_sentences, min_freq=1)

def encode_sentence(sentence, vocab):
    return [vocab.get(tok, vocab["<unk>"]) for tok in sentence]

def tensor_from_sentence(sentence, vocab):
    tokens = [vocab["<sos>"]] + encode_sentence(sentence, vocab) + [vocab["<eos>"]]
    return torch.tensor(tokens, dtype=torch.long)

class TranslationDataset(Dataset):
    def __init__(self, src_sentences, tgt_sentences, src_vocab, tgt_vocab):
        self.src_sentences = src_sentences
        self.tgt_sentences = tgt_sentences
        self.src_vocab = src_vocab
        self.tgt_vocab = tgt_vocab

    def __len__(self):
        return len(self.src_sentences)

    def __getitem__(self, idx):
        src_tensor = tensor_from_sentence(self.src_sentences[idx], self.src_vocab)
        tgt_tensor = tensor_from_sentence(self.tgt_sentences[idx], self.tgt_vocab)
        return src_tensor, tgt_tensor

def collate_fn(batch):
    src_batch, tgt_batch = zip(*batch)
    src_batch = pad_sequence(src_batch, padding_value=src_vocab["<pad>"], batch_first=True)
    tgt_batch = pad_sequence(tgt_batch, padding_value=tgt_vocab["<pad>"], batch_first=True)
    return src_batch, tgt_batch

BATCH_SIZE = 8  # зменшено для економії пам'яті
train_dataset = TranslationDataset(train_src_sentences, train_tgt_sentences, src_vocab, tgt_vocab)
val_dataset = TranslationDataset(val_src_sentences, val_tgt_sentences, src_vocab, tgt_vocab)
train_loader = DataLoader(train_dataset, batch_size=BATCH_SIZE, shuffle=True, collate_fn=collate_fn)
val_loader = DataLoader(val_dataset, batch_size=BATCH_SIZE, shuffle=False, collate_fn=collate_fn)

# Модель Encoder-Decoder з увагою (зменшені розміри для пам'яті)
class Encoder(nn.Module):
    def __init__(self, input_dim, emb_dim, hid_dim, n_layers, dropout):
        super().__init__()
        self.embedding = nn.Embedding(input_dim, emb_dim)
        self.rnn = nn.LSTM(emb_dim, hid_dim, n_layers, dropout=dropout, batch_first=True)
        self.dropout = nn.Dropout(dropout)

    def forward(self, src):
        embedded = self.dropout(self.embedding(src))
        outputs, (hidden, cell) = self.rnn(embedded)
        return outputs, hidden, cell

class Attention(nn.Module):
    def __init__(self, hid_dim):
        super().__init__()
        self.attn = nn.Linear(hid_dim * 2, hid_dim)
        self.v = nn.Linear(hid_dim, 1, bias=False)

    def forward(self, hidden, encoder_outputs, mask=None):
        batch_size = encoder_outputs.shape[0]
        src_len = encoder_outputs.shape[1]

        hidden = hidden.unsqueeze(1).repeat(1, src_len, 1)
        energy = torch.tanh(self.attn(torch.cat((hidden, encoder_outputs), dim=2)))
        attention = self.v(energy).squeeze(2)

        if mask is not None:
            attention = attention.masked_fill(mask == 0, -1e10)
        return torch.softmax(attention, dim=1)

class Decoder(nn.Module):
    def __init__(self, output_dim, emb_dim, hid_dim, n_layers, dropout, attention):
        super().__init__()
        self.output_dim = output_dim
        self.embedding = nn.Embedding(output_dim, emb_dim)
        self.rnn = nn.LSTM(emb_dim + hid_dim, hid_dim, n_layers, dropout=dropout, batch_first=True)
        self.fc_out = nn.Linear(hid_dim * 2, output_dim)
        self.dropout = nn.Dropout(dropout)
        self.attention = attention

    def forward(self, input, hidden, cell, encoder_outputs, mask=None):
        input = input.unsqueeze(1)
        embedded = self.dropout(self.embedding(input))
        attn_weights = self.attention(hidden[-1], encoder_outputs, mask)
        attn_weights = attn_weights.unsqueeze(1)
        weighted = torch.bmm(attn_weights, encoder_outputs)
        rnn_input = torch.cat((embedded, weighted), dim=2)
        output, (hidden, cell) = self.rnn(rnn_input, (hidden, cell))
        output = output.squeeze(1)
        weighted = weighted.squeeze(1)
        prediction = self.fc_out(torch.cat((output, weighted), dim=1))
        return prediction, hidden, cell, attn_weights.squeeze(1)

class Seq2Seq(nn.Module):
    def __init__(self, encoder, decoder, src_pad_idx, device):
        super().__init__()
        self.encoder = encoder
        self.decoder = decoder
        self.src_pad_idx = src_pad_idx
        self.device = device

    def create_mask(self, src):
        mask = (src != self.src_pad_idx).to(self.device)
        return mask

    def forward(self, src, trg, teacher_forcing_ratio=0.5):
        batch_size = src.shape[0]
        trg_len = trg.shape[1]
        trg_vocab_size = self.decoder.output_dim

        outputs = torch.zeros(batch_size, trg_len, trg_vocab_size).to(self.device)
        encoder_outputs, hidden, cell = self.encoder(src)
        input = trg[:,0]
        mask = self.create_mask(src)

        attentions = torch.zeros(batch_size, trg_len, src.shape[1]).to(self.device)

        for t in range(1, trg_len):
            output, hidden, cell, attention = self.decoder(input, hidden, cell, encoder_outputs, mask)
            outputs[:,t] = output
            attentions[:,t] = attention
            teacher_force = random.random() < teacher_forcing_ratio
            top1 = output.argmax(1)
            input = trg[:,t] if teacher_force else top1

        return outputs, attentions

INPUT_DIM = len(src_vocab)
OUTPUT_DIM = len(tgt_vocab)
ENC_EMB_DIM = 128
DEC_EMB_DIM = 128
HID_DIM = 128
N_LAYERS = 1
ENC_DROPOUT = 0.3
DEC_DROPOUT = 0.3
SRC_PAD_IDX = src_vocab["<pad>"]

attn = Attention(HID_DIM)
enc = Encoder(INPUT_DIM, ENC_EMB_DIM, HID_DIM, N_LAYERS, ENC_DROPOUT)
dec = Decoder(OUTPUT_DIM, DEC_EMB_DIM, HID_DIM, N_LAYERS, DEC_DROPOUT, attn)

model = Seq2Seq(enc, dec, SRC_PAD_IDX, device).to(device)

criterion = nn.CrossEntropyLoss(ignore_index=tgt_vocab["<pad>"])
optimizer = optim.Adam(model.parameters())

def train(model, dataloader, optimizer, criterion, clip=1):
    model.train()
    epoch_loss = 0
    for src, trg in dataloader:
        src, trg = src.to(device), trg.to(device)
        optimizer.zero_grad()
        output, _ = model(src, trg)
        output_dim = output.shape[-1]
        output = output[:,1:].reshape(-1, output_dim)
        trg = trg[:,1:].reshape(-1)
        loss = criterion(output, trg)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), clip)
        optimizer.step()
        epoch_loss += loss.item()
    # Очистка пам'яті після епохи
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    return epoch_loss / len(dataloader)

def evaluate(model, dataloader, criterion):
    model.eval()
    epoch_loss = 0
    with torch.no_grad():
        for src, trg in dataloader:
            src, trg = src.to(device), trg.to(device)
            output, _ = model(src, trg, teacher_forcing_ratio=0.4)
            output_dim = output.shape[-1]
            output = output[:,1:].reshape(-1, output_dim)
            trg = trg[:,1:].reshape(-1)
            loss = criterion(output, trg)
            epoch_loss += loss.item()
    return epoch_loss / len(dataloader)

N_EPOCHS = 3
train_losses = []
val_losses = []

for epoch in range(N_EPOCHS):
    train_loss = train(model, train_loader, optimizer, criterion)
    val_loss = evaluate(model, val_loader, criterion)
    train_losses.append(train_loss)
    val_losses.append(val_loss)
    print(f"Epoch {epoch+1}/{N_EPOCHS} — Train loss: {train_loss:.3f} — Val loss: {val_loss:.3f}")

plt.figure(figsize=(10,6))
plt.plot(range(1,N_EPOCHS+1), train_losses, label="Train loss")
plt.plot(range(1,N_EPOCHS+1), val_losses, label="Validation loss")
plt.xlabel("Epoch")
plt.ylabel("Loss")
plt.title("Loss during training")
plt.legend()
plt.grid()
plt.show()

inv_tgt_vocab = {v:k for k,v in tgt_vocab.items()}
inv_src_vocab = {v:k for k,v in src_vocab.items()}

def translate_sentence(sentence, src_vocab, tgt_vocab, model, max_len=50):
    model.eval()
    tokens = tokenize_src(sentence)
    tokens_idx = [src_vocab.get(tok, src_vocab["<unk>"]) for tok in tokens]
    src_tensor = torch.tensor([src_vocab["<sos>"]] + tokens_idx + [src_vocab["<eos>"]], dtype=torch.long).unsqueeze(0).to(device)
    trg_indexes = [tgt_vocab["<sos>"]]
    attentions = []
    encoder_outputs, hidden, cell = model.encoder(src_tensor)
    mask = model.create_mask(src_tensor)
    input_token = torch.tensor([tgt_vocab["<sos>"]], dtype=torch.long).to(device)
    for _ in range(max_len):
        output, hidden, cell, attention = model.decoder(input_token, hidden, cell, encoder_outputs, mask)
        pred_token = output.argmax(1).item()
        trg_indexes.append(pred_token)
        attentions.append(attention.cpu().detach().numpy())
        if pred_token == tgt_vocab["<eos>"]:
            break
        input_token = torch.tensor([pred_token], dtype=torch.long).to(device)
    trg_tokens = [inv_tgt_vocab.get(i, "<unk>") for i in trg_indexes]
    return trg_tokens[1:], attentions

def plot_attention(sentence, translation, attention):
    fig = plt.figure(figsize=(10,10))
    ax = fig.add_subplot(111)
    attention = np.array(attention)
    attention = attention.squeeze(1)
    cax = ax.matshow(attention, cmap='bone')
    fig.colorbar(cax)
    ax.set_xticklabels([''] + sentence, rotation=90)
    ax.set_yticklabels([''] + translation)
    ax.xaxis.set_major_locator(plt.MultipleLocator(1))
    ax.yaxis.set_major_locator(plt.MultipleLocator(1))
    plt.show()

test_sentences = [
    "Europa-Parlamentet er den direkte valgte lovgivende forsamling i Den Europæiske Union.",
    "Vi mener, at fred og velstand i Europa kun kan opnås gennem samarbejde.",
    "Denne aftale vil styrke relationerne mellem vores lande."
]

for sent in test_sentences:
    translation, attention = translate_sentence(sent, src_vocab, tgt_vocab, model)
    print(f"\nSource: {sent}")
    print("Translation:", " ".join(translation).replace("<eos>", "").strip())
    plot_attention(tokenize_src(sent), translation[:10], attention[:10])

print("""
Коментар щодо роботи механізму уваги (attention mechanism):

Механізм уваги дозволяє моделі фокусуватись на найбільш релевантних частинах вхідного речення під час генерації кожного слова вихідного речення.
Це особливо корисно для перекладу довгих речень, де звичайний Seq2Seq без уваги стикається з втратою контексту.

Пояснення:
- Під час перекладу кожного слова в цільовій мові, механізм уваги обчислює ваги (attention weights) для кожного слова джерельного речення.
- Ці ваги показують, скільки "уваги" потрібно приділити кожному слову вхідного речення.
- В результаті модель генерує більш точний і контекстно залежний переклад.

Візуалізація уваги також може допомогти інтерпретувати, як модель "розуміє" відповідність між словами.

""")