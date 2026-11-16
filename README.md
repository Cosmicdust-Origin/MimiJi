# MimiJi

일본어 영상에서 한국어 자막을, 한국어 영상에서 일본어 자막을 만드는 Windows 데스크톱 앱입니다. 음성 인식(전사)은 내 PC의 GPU에서
처리하고, 번역은 내가 구독 중인 ChatGPT(Codex CLI) 또는 Claude(Claude Code) 계정으로 합니다.
API 키를 쓰지 않으며, 앱은 로그인 정보를 저장하지 않습니다.

*MimiJi is a Windows desktop app that turns Japanese video into Korean subtitles (and Korean video into Japanese subtitles). Speech is
transcribed locally with faster-whisper large-v3 on your NVIDIA GPU; translation runs through
the official Codex CLI (ChatGPT subscription) or Claude Code (Claude subscription) you are
already signed in to. No API keys, no server, nothing uploaded except the transcript text you
send to the translation model.*

## 무엇을 하나요

- 영상 → 전사(30초 구간 체크포인트, 끊겨도 이어하기) → 번역 → SRT 자막과 작업 보고서.
  언어 방향은 일본어 → 한국어, 한국어 → 일본어 중에서 고릅니다.
- 음성 감지(VAD)가 배경음에 묻힌 대사를 놓친 구간은 VAD 없이 다시 전사해 살립니다.
- 번역은 줄 번호(raw_id) 단위로 검증합니다. 빠진 줄, 한국어에 남은 일본어, 확신이 낮은 줄은 다시
  요청하고, 끝까지 애매한 줄은 "번역 검토" 화면에서 채택·수정·삭제할 수 있습니다.
- 번역 배치를 여러 개 동시에 보내 시간을 줄입니다(예: 2시간 영상 약 15분, 실측은 영상마다 다름).

## 필요한 것

- Windows 10/11, NVIDIA GPU와 최신 드라이버, CUDA 12 런타임(cuBLAS). GPU가 없으면 CPU 전사를
  직접 고를 수 있지만 매우 느립니다.
- FFmpeg(`ffprobe`가 PATH에 있어야 함)
- 번역 계정: [Codex CLI](https://github.com/openai/codex) + ChatGPT 구독, 또는
  [Claude Code](https://github.com/anthropics/claude-code) + Claude 구독. 번역에는 본인 구독의
  사용량이 쓰입니다.
- 전사 모델 faster-whisper large-v3(약 3.1GB, 첫 실행 진단 화면에서 받습니다)

이 프로젝트는 OpenAI, Anthropic과 관계없는 개인 프로젝트입니다. 자막을 만들 권리가 있는 영상에만
사용하십시오.

## 라이선스

소스 코드는 [MIT 라이선스](LICENSE)입니다. 다만 배포하는 Windows 실행 파일 묶음에는 GPL 구성으로
빌드된 FFmpeg 라이브러리(PyAV 동봉)가 들어 있어, 그 묶음은 GPL-2.0-or-later 조건을 따릅니다.
자세한 내용은 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)를 참고하십시오.

## 버전

현재 버전과 변경 내역은 `CHANGELOG.md`를 참고합니다. 버전은
`src/local_subtitle_tool/_version.py` 한 곳에서만 관리합니다. 이전 이름은 PolyCue(0.3.0~0.7.x),
그 전에는 Local Subtitle Tool(0.2.x까지)이었으며, 처음 실행하면 이전 데이터 폴더의 작업 목록과
설정을 `%LOCALAPPDATA%\MimiJi`로 복사합니다. 내부 Python 패키지 이름은 `local_subtitle_tool`입니다.

## 설계 요약

- 전사는 기본적으로 `faster-whisper large-v3`, `cuda:0`, `float16`을 사용합니다.
- CUDA가 없을 때 자동으로 CPU로 바꾸지 않습니다. 사용자가 CPU를 명시적으로 선택하면
  `large-v3`, `int8`과 자동 계산된 스레드 수로 실행합니다.
- 번역은 기본적으로 ChatGPT 로그인 상태의 Codex CLI와 `gpt-5.6-luna`를 사용합니다.
- Claude Code의 Claude 구독 로그인과 `sonnet`도 선택할 수 있습니다.
- 제공자, 모델, 사고 수준, Standard/Fast 모드는 번역 캐시 지문에 포함됩니다. 설정이나
  일본어 입력, 화자 문맥이 바뀌면 이전 배치를 재사용하지 않습니다. 다만 사용량 한도 또는
  로그인 문제로 중단된 작업에서 사용자가 명시적으로 "설정 바꿔서 이어하기"를 선택하면,
  원래 실행 프로필로 지문을 다시 검증한 완료 배치만 유지하고 남은 배치에 새 설정을 적용합니다.
- 모델이 특정 배치를 명시적으로 거절하면 해당 배치만 보류하고 새 독립 세션으로 다음
  배치를 계속합니다. 보류가 남아 있는 동안 최종 SRT는 생성하지 않습니다.
- 모델이 `review`로 보류한 항목은 같은 제공자·모델로 문맥을 넓혀 소규모 배치, 이어서 단건으로
  자동 보강 번역합니다. 그래도 남은 항목은 `PARTIAL_REVIEW` 상태로 두고 검토 화면에서 다시
  번역하거나 직접 입력합니다. 미해결 항목이 있으면 최종 SRT를 출력 폴더로 내보내지 않습니다.
- 보류된 배치는 사용자가 "보류 배치 다시 요청"을 누르거나 `--retry-held`를 줄 때만 다시
  요청합니다. 거절된 내용을 자동으로 다시 보내거나 다른 제공자로 돌리지 않습니다.
- 앱은 계정 토큰이나 API 키를 직접 저장하지 않습니다.
- Codex는 작업마다 하나의 지속 세션을 사용합니다(`codex exec --json` → `codex exec resume`).
  세션 ID는 대화 ID일 뿐 인증 정보가 아니며 작업 manifest에 저장됩니다. 지속 세션의 대화 기록은
  Codex가 `~/.codex/sessions`에 보관합니다. 거절이 나오면 그 세션을 폐기하고 다음 배치를 새 세션으로
  시작하며, 실행 프로필이 바뀌거나 세션이 길어지면 새 세션으로 교체합니다. 고급 설정에서 끌 수 있습니다.
- 응답에서 일부 raw_id만 틀리면 정상 항목은 보존하고 틀린 raw_id만 다시 요청합니다.
- 번역 응답은 압축 형식입니다. 모델은 번역한 줄의 한국어만 쓰고, 일본어 보정·생략·검토는
  해당 줄만 따로 적습니다. 인식 신뢰도가 낮은 줄은 일본어 보정을 먼저 쓰게 하고, 실제 말이 있는
  줄을 생략하거나 한국어에 일본어 가나·한자를 남기면 그 줄만 다시 요청합니다.
- 요청마다 첫 응답 시간, 토큰 사용량, 세션 정보를 `provider_events.jsonl`과 결과 보고서에 남깁니다.
  Codex CLI가 실제 적용된 service tier를 알려 주지 않으므로 Fast 적용 여부는 "확인 불가"로 표시합니다.
- 전사는 30초 클립별 체크포인트, 번역은 배치별 결과를 보존하여 누락된 구간부터 재개합니다.
- Silero VAD(`min_silence_duration_ms=700`, `speech_pad_ms=150`)를 영상 전체에 적용하고,
  30초 창마다 음성 구간만 원본 오디오 그대로 디코딩합니다. 2초 이하의 짧은 무음은 같은
  구간으로 묶어 타임스탬프를 정확히 유지합니다.
- 원본 전사는 원본 영상과 전사 정책 지문이 일치할 때만 재사용합니다.
- 미완료 작업은 `%LOCALAPPDATA%\MimiJi\jobs.json`에서 찾습니다. 작업 목록은
  색인만 보관하며 실제 전사·번역 데이터는 각 작업 폴더에 유지합니다.
- 첫 실행에서는 하드웨어, FFmpeg, 전사 패키지, CUDA/CPU 실행 가능 여부, 전사 모델,
  Codex와 Claude 로그인을 자동 진단합니다. 이후에는 환경 진단 화면에서 다시 검사할 수
  있습니다.
- Windows의 `.cmd`와 `.ps1` CLI는 감지된 절대 경로와 적절한 실행기를 사용합니다.
- 전사는 처리한 미디어 시각 기준, 번역은 완료한 배치 기준으로 진행률을 표시합니다.
- 전사·번역 중에는 다른 단계로 이동할 수 없으며 중지 버튼으로 안전하게 중단할 수 있습니다.
- 번역 중지는 실행 중인 CLI 자식 프로세스까지 종료하고 완료된 배치는 재개용으로 보존합니다.
- GUI는 PySide6/QML 기반의 Windows 네이티브 앱입니다. 새 작업 화면에서 영상, 번역 모델,
  사고 수준, 처리 속도와 출력 폴더를 한 번에 설정하고 실행 중 화면에서 전체 진행률,
  단계별 상태, 경과 시간, 예상 잔여 시간과 최근 로그를 확인합니다.
- 작업 데이터와 빌드 결과는 출력 폴더의 `.subtitle_job`에 두고, 검증을 통과한 6개 결과
  파일만 한 묶음으로 출력 폴더에 게시합니다. 이후 실행이 부분 완료·검토 필요·검증 실패로 끝나면
  앱이 게시했던 파일을 `.subtitle_job/superseded`로 옮깁니다(삭제하지 않음).
- 오류는 `%LOCALAPPDATA%\MimiJi\logs\app.log`에 기록합니다.
- Codex/Claude 계정은 고급 설정의 "번역 계정"에서 상태 확인, 로그인·계정 전환, 로그아웃을
  합니다. 새 작업 화면에는 선택한 제공자의 로그인 상태가 표시됩니다. 로그인과 로그아웃은
  공식 CLI(`codex login`/`codex logout`, `claude auth login`/`claude auth logout`)가 처리합니다.
- 전용 GPU가 없을 때는 고급 설정에서 CPU `int8` 전사를 명시적으로 선택할 수 있습니다.
- 화자 분리는 선택 기능입니다. 기본 설치와 전사 결과에는 영향을 주지 않으며, 켜면
  `pyannote/speaker-diarization-community-1`의 화자 구간을 large-v3 단어 타임스탬프와
  연결해 번역 문맥과 큐 경계에 사용합니다. SRT 화자 라벨은 기본적으로 표시하지 않습니다.

## 설치

현재 검증한 권장 Python은 3.14입니다. 다른 지원 버전을 사용하려면 같은 인터프리터에
PyAV, CTranslate2, faster-whisper를 설치해야 합니다.

```powershell
cd local_subtitle_tool
py -3.14 -m pip install -e .
```

## 실행

다음 명령으로 실행합니다.

```powershell
py -3.14 -m local_subtitle_tool gui
```

Windows 실행 파일로 패키징하는 방법은 `packaging/README.md`를 참고합니다.

환경만 확인하려면 다음 명령을 사용합니다.

```powershell
py -3.14 -m local_subtitle_tool diagnose --video "C:\path\video.mp4"
```

전사부터 검증까지 실행하려면 다음과 같이 사용합니다.

```powershell
py -3.14 -m local_subtitle_tool run `
  --video "C:\path\video.mp4" `
  --code "작품 제목" `
  --output "C:\path\작품 제목" `
  --provider codex `
  --model gpt-5.6-luna `
  --reasoning-effort xhigh `
  --speed-mode fast
```

`fast`는 Codex CLI에서만 지원합니다. Claude를 선택할 때는 `--speed-mode standard`를
사용해야 합니다.

CUDA를 사용할 수 없는 컴퓨터에서 CPU 전사를 명시적으로 허용하려면 다음 옵션을
추가합니다. CPU 전사는 상당히 느릴 수 있습니다.

```powershell
  --accelerator cpu --compute-type int8
```

## 선택형 화자 분리

화자 분리는 기본 설치에서 제외됩니다. GUI의 `고급 설정`에서 다음 순서로 준비할 수 있습니다.
PyTorch와 관련 런타임이 함께 설치되므로 환경에 따라 수 GB의 디스크 공간이 필요할 수 있습니다.

1. 선택 구성 요소를 설치합니다.
2. [community-1 이용 조건](https://huggingface.co/pyannote/speaker-diarization-community-1)에
   동의합니다.
3. 공식 `hf auth login`으로 로그인합니다. 토큰은 앱이 직접 저장하지 않습니다.
4. `모델 받기`를 눌러 로컬 캐시를 준비합니다.

명령줄에서 선택 구성 요소만 설치하려면 다음 명령을 사용합니다.

```powershell
py -3.14 -m pip install -e ".[diarization]"
```

기본 정렬은 ASR 세그먼트 단위로 화자를 할당합니다. 정밀 정렬은 large-v3가 이미 생성한
단어 타임스탬프에 화자를 연결합니다. 별도의 강제 정렬 모델을 추가로 다운로드하지 않습니다.

CLI에서도 다음 옵션을 사용할 수 있습니다.

```powershell
  --diarization `
  --diarization-speakers 2 `
  --diarization-alignment precise `
  --diarization-device auto
```

## 작업 재개

미완료 작업 목록과 재개 가능 상태는 다음 명령으로 확인합니다.

GUI에서는 번역 사용량 한도 또는 로그인 문제로 중단된 작업에 한해 기존 설정으로 이어가거나
번역 설정을 바꿔서 이어갈 수 있습니다. 자동 제공자 전환은 하지 않으며, 거절 또는
`INVALID_OUTPUT`으로 보류된 배치는 설정을 바꿔도 자동으로 다시 요청하지 않습니다.

```powershell
py -3.14 -m local_subtitle_tool jobs
py -3.14 -m local_subtitle_tool inspect-job <JOB_ID>
py -3.14 -m local_subtitle_tool resume <JOB_ID>
```

목록에서만 제거하려면 다음 명령을 사용합니다. 영상, 출력물, 체크포인트는 삭제하지 않습니다.

```powershell
py -3.14 -m local_subtitle_tool remove-job <JOB_ID>
```

## 인증

GUI의 로그인 버튼은 다음 공식 명령을 별도 콘솔에서 실행합니다.

- Codex: `codex login`
- Claude: `claude auth login`

로그인 이후에는 각각 `codex login status`, `claude auth status --json`으로 상태만 확인합니다.

도구는 구독 범위 사용을 보장하기 위해 다음 상태만 허용합니다.

- Codex: 상태 문구에 `ChatGPT` 로그인이 명시되어 있어야 합니다.
- Claude: `authMethod`가 `claude.ai`여야 합니다.
- `OPENAI_API_KEY` 또는 `ANTHROPIC_API_KEY`가 설정되어 있으면 별도 API 과금 위험 때문에
  해당 제공자의 번역을 시작하지 않습니다.

지정 모델을 사용할 수 없을 때 다른 모델로 자동 대체하지 않습니다.

## 제3자 구성 요소

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — MIT
- [CTranslate2](https://github.com/OpenNMT/CTranslate2) — MIT
- [Codex CLI](https://github.com/openai/codex) — Apache-2.0
- [Claude Code](https://github.com/anthropics/claude-code) — Anthropic 이용약관 적용, 외부 CLI로만 연동
- FFmpeg — `ffprobe`는 설치된 외부 실행 파일을 쓰고, 오디오 디코딩용 라이브러리는 PyAV에
  동봉된 것을 씁니다(위 라이선스 참고).
