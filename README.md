# flowapp — 수급분석 스캐너 (윈도우 배포판)

국내 주식 일봉·주봉·60분봉 검색식 스캐너 + 투자자별 수급(매집·분산) 차트를 한 화면에 묶은 로컬 웹 앱.

**받기:** [Releases](../../releases)에서 `flowapp-win.zip` 을 받아 압축을 풀고 `flowapp 실행.bat` 을 더블클릭.
처음 실행 때 데이터 묶음(`flowapp-data.zip`, 약 1.2GB)을 자동으로 받습니다. 자세한 사용법은 `win/사용법.txt`.

- 각자의 키움 REST API 키는 앱의 「설정」 화면에서 넣습니다(이 PC의 `%LOCALAPPDATA%\flowapp\config` 에만 저장).
- 「설정 → 업데이트 설치」는 이 저장소 `main` 의 최신 코드를 받아 바꿉니다.

투자 판단의 책임은 사용자에게 있습니다. 화면의 검정 수치는 과거 자료에 대한 것입니다.

## 개발자용
- `scan/` `bin/` 은 정본 저장소에서 `tools/sync.sh` 로 복사한 것 — 여기서 직접 고치지 않는다.
- `tools/build_win.sh` 배포 압축본 · `tools/make_data.sh` 데이터 묶음 · `tools/publish.sh` 동기화→커밋→푸시.
