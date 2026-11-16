# Third-party notices

이 문서는 MimiJi 배포본에 포함되거나 실행 시 호출되는 외부 구성 요소를 정리합니다.
배포본의 `licenses/` 폴더에는 포함된 Python 배포 패키지의 원본 라이선스 파일이 들어 있으며,
`licenses/INDEX.txt`에 패키지 이름, 버전, 라이선스 표현식이 기록되어 있습니다.

## 1. 배포본에 포함되는 구성 요소

| 구성 요소 | 용도 | 라이선스 |
|---|---|---|
| Qt 6 / PySide6 / Shiboken6 | 네이티브 GUI(QML) | LGPL-3.0 (Qt for Python) |
| faster-whisper (SYSTRAN) | Whisper 전사 | MIT |
| CTranslate2 (OpenNMT) | 추론 런타임 | MIT |
| NVIDIA cuDNN 9 (`cudnn64_9.dll`, CTranslate2 wheel 동봉) | CUDA 추론 | NVIDIA cuDNN Software License Agreement (재배포 조항 적용) |
| Intel OpenMP (`libiomp5md.dll`, CTranslate2 wheel 동봉) | CPU 병렬 처리 | Intel Simplified Software License |
| ONNX Runtime | Silero VAD 실행 | MIT |
| Silero VAD 모델 (`silero_vad_v6.onnx`, faster-whisper 동봉) | 음성 구간 검출 | MIT |
| PyAV | 오디오 디코딩 | BSD-3-Clause |
| FFmpeg 라이브러리 (`av.libs/*.dll`, PyAV wheel 동봉) | 오디오 디코딩 | **GPL-2.0-or-later** (아래 참고) |
| tokenizers, huggingface_hub | 토크나이저, 모델 캐시 | Apache-2.0 |
| NumPy | 수치 연산 | BSD-3-Clause |
| 기타 의존 패키지 | — | `licenses/INDEX.txt` 참고 |

### FFmpeg와 GPL

PyAV의 Windows wheel은 `libx264`, `libx265` 등 GPL 구성 요소와 함께 빌드된 FFmpeg DLL을
`av.libs` 폴더에 동봉합니다. 이 DLL을 포함한 배포본은 GPL-2.0-or-later의 적용을 받습니다.

- 배포본에는 FFmpeg DLL이 **포함됩니다**. 이전 문서의 "FFmpeg 바이너리를 포함하지 않습니다"
  설명은 PyAV 동봉 DLL을 고려하지 않은 것이었으며, 이 문서로 대체합니다.
- 배포본을 다른 사람에게 전달하는 경우 GPL에 따라 해당 FFmpeg 빌드의 대응 소스 코드를
  함께 제공하거나 제공 방법을 안내해야 합니다. PyAV 빌드에 사용된 FFmpeg 소스와 빌드 스크립트는
  PyAV 프로젝트(https://github.com/PyAV-Org/PyAV)와 FFmpeg(https://ffmpeg.org/)에서 받을 수 있습니다.
- GPL 구성 요소가 없는 배포가 필요하면 LGPL 구성으로 빌드한 FFmpeg/PyAV로 교체한 뒤 다시
  패키징해야 합니다.

별도로, 앱은 사용자가 설치한 `ffprobe` 실행 파일을 호출하여 영상 정보를 확인합니다. 이 실행 파일은
배포본에 포함되지 않으며 사용자가 설치한 빌드의 라이선스를 따릅니다.

## 2. 배포본에 포함되지 않고 사용자가 받는 구성 요소

| 구성 요소 | 받는 방법 | 조건 |
|---|---|---|
| faster-whisper large-v3 가중치 (`Systran/faster-whisper-large-v3`) | 앱에서 명시적 승인 후 Hugging Face 캐시에 다운로드 | MIT |
| NVIDIA 드라이버, CUDA 12 런타임(cuBLAS) | NVIDIA 공식 설치 프로그램 | NVIDIA EULA |
| OpenAI Codex CLI | 사용자 설치, 외부 프로세스로 호출 | Apache-2.0 |
| Anthropic Claude Code | 사용자 설치, 외부 프로세스로 호출(소스 복사·재배포 없음) | Anthropic Commercial Terms of Service |
| FFmpeg/ffprobe 실행 파일 | 사용자 설치 | 설치한 빌드의 라이선스 |

## 3. 화자 분리 배포판에만 포함되는 구성 요소

`MimiJi-Diarization` 배포판에는 다음이 추가로 포함됩니다. 기본 배포판에는 포함되지 않습니다.

| 구성 요소 | 라이선스 |
|---|---|
| PyTorch, torchaudio, torchcodec | BSD-3-Clause 등 (`licenses/` 참고) |
| pyannote.audio | MIT |

`pyannote/speaker-diarization-community-1` 모델 가중치는 포함하지 않습니다. 사용자가 Hugging Face에서
이용 조건에 동의하고 공식 `hf auth login`으로 로그인한 뒤 내려받습니다. 앱은 토큰을 저장하지 않습니다.
