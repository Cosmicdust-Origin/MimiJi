# 패키징 (PyInstaller onedir)

## 빌드

프로젝트 루트에서 실행합니다.

```powershell
py -3.14 -m pip install --user "pyinstaller>=6.16"
py -3.14 -m PyInstaller packaging/mimiji.spec --noconfirm
py -3.14 packaging/collect_licenses.py dist/MimiJi
```

결과: `dist/MimiJi/`

- `MimiJi.exe`: GUI(창 모드)
- `MimiJiCli.exe`: CLI(콘솔). 예: `MimiJiCli.exe --version`, `MimiJiCli.exe diagnose`
- `_internal/`: Python 런타임, Qt/QML, CTranslate2·cuDNN, ONNX Runtime, PyAV(FFmpeg DLL)
- `licenses/`, `THIRD_PARTY_NOTICES.md`: 라이선스 고지

폴더 전체를 그대로 배포합니다. 단일 파일(onefile) 빌드는 실행할 때마다 수백 MB를 임시 폴더에 풀어
시작이 느리고 백신 오탐 위험이 커서 사용하지 않습니다. UPX 압축도 같은 이유로 끕니다.

## 기본판과 화자 분리판

- 기본판에는 PyTorch와 pyannote가 포함되지 않습니다. 화자 분리 설정은
  `MimiJi-Diarization` 배포판이 필요하다고 안내하며, 화자 분리 사용·이용 조건·HF 로그인·모델 받기
  조작은 비활성화됩니다. 패키징된 앱에서 선택 구성 요소를 별도로 설치하지 않습니다.
- 화자 분리판은 `pip install .[diarization]`이 성공한 환경에서 다음과 같이 따로 빌드합니다.

```powershell
$env:LST_WITH_DIARIZATION = "1"
py -3.14 -m PyInstaller packaging/mimiji.spec --noconfirm
py -3.14 packaging/collect_licenses.py dist/MimiJi-Diarization
```

## 실행 환경 요구 사항 (배포본에 포함되지 않음)

- NVIDIA 드라이버와 CUDA 12 런타임(cuBLAS: `cublas64_12.dll`, `cublasLt64_12.dll`)이 DLL 검색 경로에
  있어야 CUDA 전사를 사용할 수 있습니다. 없으면 진단 화면이 CUDA를 "조치 필요"로 표시하며,
  CPU int8 전사는 사용자가 명시적으로 선택해야 합니다.
- FFmpeg의 `ffprobe`
- 번역용 Codex CLI 또는 Claude Code(공식 설치 프로그램의 `.exe` 권장)
- 전사 모델은 첫 실행 진단에서 약 3.1GB 용량, 실제 Hugging Face 캐시 경로와 여유 공간을
  확인하고 명시적으로 승인한 뒤 받습니다.

## 패키징 후 스모크 테스트

1. 새 Windows 계정(또는 Windows Sandbox)에서 `MimiJi.exe` 실행 → 창이 뜨고 첫 진단이 시작됨
2. `%LOCALAPPDATA%\MimiJi\logs\app.log` 생성 확인
3. HF 캐시가 없는 상태에서 화자 분리 상태가 "구성 요소 없음"으로 표시되고 앱이 계속 동작
4. large-v3 다운로드 → 진단 재실행 → 모델 준비 표시
5. cuBLAS가 없는 PC: CUDA "조치 필요", 자동 CPU 전환 없음, CPU 명시 선택 후 전사
6. Codex/Claude 로그인 버튼이 공식 로그인 창을 엶
7. 전사 클립 N에서 중지 → 앱 재시작 → 이전 작업에서 재개 → raw_id 연속
8. 번역 배치 N에서 중지 → `codex.exe`/`claude.exe` 프로세스가 남지 않음
9. 작업 중 창 닫기 → 확인 창 → 안전 지점 저장 후 종료, 자식 프로세스 없음
10. 한글·공백이 들어간 OneDrive 경로에 출력
11. review 항목: 검토 화면에서 다시 번역/직접 입력 → 검증·출력 → 6개 파일 게시
12. 980×680 창에서 중지 버튼과 상태 표시가 보임
13. 결과 파일 링크 열기
