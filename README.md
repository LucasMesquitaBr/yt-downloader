# 🎬 YouTube Downloader

Site local para baixar vídeos do YouTube em qualquer resolução.

## ✅ Pré-requisitos

- **Python 3.9+** → [python.org/downloads](https://www.python.org/downloads/)
  - Durante a instalação: marque ☑ **"Add Python to PATH"**
- **ffmpeg** (opcional, mas recomendado para mesclar vídeo+áudio em alta qualidade)
  - Baixe em: [ffmpeg.org](https://ffmpeg.org/download.html) ou via `winget install ffmpeg`

## 🚀 Como usar

### Opção 1 — Script automático (mais fácil)
Dê duplo clique em **`iniciar.bat`**

Ele irá:
1. Criar um ambiente virtual Python
2. Instalar Flask e yt-dlp automaticamente
3. Abrir o browser em `http://localhost:5000`

### Opção 2 — Manual (PowerShell)
```powershell
# Na pasta do projeto:
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python app.py
```
Depois acesse: http://localhost:5000

## 🖥 Como funciona

1. **Cole o link** do YouTube no campo de busca
2. O servidor busca os **metadados** (título, thumbnail, formatos disponíveis)
3. **Escolha a resolução** — de 144p até 4K, incluindo opção de áudio MP3
4. Clique em **Baixar** — o arquivo é processado no servidor e enviado ao browser

## 📁 Arquivos

| Arquivo | Descrição |
|---|---|
| `app.py` | Backend Flask + yt-dlp |
| `index.html` | Frontend (tema escuro estilo YouTube) |
| `requirements.txt` | Dependências Python |
| `iniciar.bat` | Script de instalação e inicialização |

## ⚠️ Aviso Legal

Use apenas para vídeos com permissão de download (Creative Commons, seus próprios vídeos, etc.).
