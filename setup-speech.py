"""Download public model weights once; audio is never sent to the download host."""
from pathlib import Path
from huggingface_hub import snapshot_download
import argparse

if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--model',choices=['base','small'],default='small')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    path = args.output or Path(__file__).resolve().parent/('models/whisper-'+args.model)
    snapshot_download('Systran/faster-whisper-'+args.model,local_dir=path,
        allow_patterns=['config.json','model.bin','tokenizer.json','vocabulary.*','preprocessor_config.json'])
    print('Offline speech model ready:',path)
