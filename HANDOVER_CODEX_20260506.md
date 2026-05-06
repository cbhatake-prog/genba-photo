# Codex 引き継ぎメモ 2026-05-06

## プロジェクト

- 名称: 現場フォト管理 / genba-photo
- GitHub: https://github.com/cbhatake-prog/genba-photo.git
- 作業ブランチ: `voice-memo-mobile-ui-template`
- NAS本番パス: `\\192.168.3.167\genba_photo`
- ローカルclone: `C:\Users\hatak\Documents\Codex\2026-05-06\codex-codex-claude-code-codex-ai\github_pr_genba_photo`
- 公開現場URL: `https://photo.j-cb.com/p/W31R3HrI1k0b-g?admin=1`

## 重要な運用注意

- NAS本番ファイルへ直接上書きすると、過去に0バイト化したことがある。
- 本番反映は必ず「一時ファイルへコピー -> サイズ確認 -> Move/Replace -> 再確認」で行う。
- Codex側PCでは `docker` が使えず、NASへのSSHも権限不足。コンテナ再起動はSynology Container Managerで手動実行が必要。
- 秘密情報はGitへ入れない。LINE Channel SecretはNASの設定ファイルにのみ置く。

## 直近のGitコミット

- `44e5f92` Handle duplicate LINE login callbacks
- `06d372d` Improve LINE login error handling
- `b268563` Make LINE login callback stateless
- `e22bafc` Read LINE login config from data volume
- `c9f7673` Add LINE login worker identity
- `67861eb` Harden kokuban camera image compression
- `fc2bc37` Optimize kokuban camera uploads
- `c546e44` Guard voice memo against oversized speech counts

## LINEログイン

### 現状

- ユーザー確認でLINEログインは成功済み。
- Channel ID: `2009985135`
- Callback URL: `https://photo.j-cb.com/auth/line/callback`
- Secretは表示・転記しない。NAS設定ファイルに保存済み。
- 設定ファイル: `\\192.168.3.167\genba_photo\data\line_login.json`

### 実装済み

- `app.py` にLINEログインルート追加:
  - `/auth/line/login`
  - `/auth/line/callback`
  - `/auth/line/nickname`
  - `/auth/line/me`
  - `/auth/line/logout`
- LINE認証後、業者名/出退勤/メモ投稿者名をニックネームで運用できる。
- OAuth stateを署名付きに変更し、LINEアプリとブラウザをまたいでも戻れるようにした。
- Android等でcallbackが二重に走る場合に備えて、短時間の成功キャッシュを追加。
- LINE失敗時のデバッグログ:
  - `\\192.168.3.167\genba_photo\uploads\_meta\line_login_debug.log`

### 失敗時の見方

- `invalid_client`: Channel ID / Secretの不一致。
- `invalid_grant`: 認証コードの期限切れ、再利用、二重callback。今回ここで詰まったが、対策後に成功。

## 数量 音声メモ

### ユーザー要件

- 現場の職人が手を使わず、マイク開始後にしゃべりっぱなしで登録したい。
- 例:
  - `5300 3本`
  - `3200 1本`
  - `420 1本`
  - `3200 3本、次、420 1本、次...`
- 寸法はmmの生値。1mmから100000mmまであり、桁補正で勝手に変えない。
- 2桁も1桁も実寸としてありえるため、`53 -> 5300` のような自動補正は禁止。
- 多いときは寸法と数が100件を超える。
- 最後に合計数量を出す。
- PDF書き出しが必要。
- iPhoneだけでなくAndroidも対象。

### 現在のファイル

- Git/NASテンプレート: `templates/voice_memo.html`
- 現在表示バージョン: `v20260506-1220`
- NAS本番: `\\192.168.3.167\genba_photo\templates\voice_memo.html`

### 実装済み

- Android Chrome/EdgeはWeb Speech APIを使う。
- iOSはWeb Speech APIが基本使えないため、入力欄モードへフォールバック。
- 音声finalを650ms待ってまとめて解析。
- `MAX_AUTO_COUNT = 200` により、誤認識の巨大本数を自動登録しない。
- `32003本` のような寸法+本数の結合を分解する補正あり。
- テキストパーサーの仮想ユーザーテスト:
  - `node tools/voice_memo_virtual_users_test.js`
  - 100/100 成功

### まだ残っている課題

- テキストテストは通るが、実音声ASRではまだ不安定。
- ユーザー報告:
  - 1個読み取ったら止まる。
  - `3400 4本` が途中で止まる/変な数字になる。
  - `380 4本` が `3800 4本` のようになることがある。
  - 現場ではもっと速く連続で読む。
- 次のAIは「実音声/合成音声でWeb Speech相当の連続テスト」を優先すること。
- テストページはユーザーが途中で見て指摘できる形が望ましい。

### 次にやるべきこと

1. `voice_memo.html` の連続認識をさらに安定化する。
2. `onend` 後の再開を速くし、発話ごとの処理を詰まらせない。
3. finalを長く溜めすぎず、1組成立したら即追加、未完成寸法だけ保留する。
4. 実音声サンプルと合成音声の両方でテストする。
5. スマホUIを大きく、見やすく、現場で押し間違えない形へ改修する。

## 黒板カメラ

### 現状

- ファイル: `templates/kokuban_camera.html`
- 高画質を保ちつつ、保存が重すぎる問題を緩和済み。
- 撮影日時/EXIF日時を黒板帯に反映。
- 後からアップロードした写真にも黒板帯を付け、撮影日時を表示する方向で実装済み。

### 主な調整

- カメラ要求を現実的な解像度に調整。
- 長辺2560px、JPEG 0.88を基本にして、8MB超なら段階的に圧縮。
- EXIF DateTimeOriginalを読む。
- Headlessテスト 36/36 成功済み。

## Google Drive共有

- Driveフォルダ: `Codex_スマホスクショ`
- フォルダID: `1aZuLvKMj-ToSlqpj1IGyoCfITheDMO4p`
- スマホスクショや録画をユーザーがここへ入れる。
- 直近LINEデバッグでは以下を確認:
  - `Screenshot_20260506_153906_Chrome.jpg`
  - `Screenshot_20260506_155645_Chrome.jpg`

## 次AIへの短い指示

まずは音声メモに集中する。LINEログインはユーザー確認で成功済みなので、戻らなくてよい。  
音声メモはテキストパースだけで満足せず、実音声または合成音声で「早口・連続・100件超」を再現して潰す。  
本番反映後は必ずNASへ安全コピーし、ユーザーにコンテナ再起動が必要かどうかを明確に伝える。
