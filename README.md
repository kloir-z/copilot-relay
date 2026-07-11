# M365 Copilot 手動リレー・コーディングエージェント

チャットしかできない M365 Copilot（等のチャットAI）を、人間がコピペで仲介する
擬似コーディングエージェントとして使うためのツール一式。

## 仕組み（30秒版）

    Copilot（脳） ⇄ あなた（手と目） ⇄ agent.py（実行係） ⇄ 実repo / git

1. Copilot が「やりたいこと」をセンチネル形式のペイロード
   （`-----OPS-----` 〜 `-----END-----`）で出す
2. あなたがそれをコピーして `in.md` に貼り、`python agent.py` を実行
3. agent.py が read / write / diff適用 / コマンド実行をこなし、
   結果＋現在のgit状態＋STATE.md を `out.txt` にまとめる
4. `out.txt` を Copilot に貼り戻す（大きければファイル添付）
5. verify（テスト）が緑になるまで繰り返し

コードフェンス（バッククォート）はプロトコル上の意味を持たない。
コピーボタンの整形済みテキストでフェンスが消えても、センチネル行が生き残るので
そのまま貼れる——これがこの方式の核。

## ファイルの役割

| ファイル | 役割 |
|---|---|
| `agent.py` | ドライバ本体（標準ライブラリのみ）。プロトコル仕様は冒頭docstringにも記載 |
| `BOOTSTRAP.md` | **Copilotに教える側**のルール（意図的に英語。理由はRUNBOOKのセットアップ節）。Agent Builder の Instructions に貼る（または新チャット冒頭に貼る） |
| `RUNBOOK.md` | **人間側**の運用手順書。セットアップ・毎ターンの手順・トラブル対応。★迷ったらここ |
| `sandbox/` `sandbox2/` `sandbox3/` | ローカル練習場（このリポジトリには含めていない）。作り方の例は RUNBOOK の「仮運用」節 |

## クイックスタート（既存repoで使う）

1. repoルートに `agent.py` を置き、`agent.config.json` を作る:

       { "verify": "pytest -q", "auto_commit": true }

2. 専用ブランチを切る: `git switch -c agent/xxx`
3. `BOOTSTRAP.md` の中身を Copilot の Agent Builder（無ければ新チャット冒頭）に貼る
4. タスクを伝える → 返信をコピー → `in.md` に貼る → `python agent.py`
   → `out.txt` を貼り返す
5. 以後ループ。作業の区切り（verify緑）ごとに `python agent.py resume` で
   引き継ぎパケットを作り、新しいチャットに移る

## コマンド早見表

| コマンド | 何をするか |
|---|---|
| `python agent.py` | `in.md` のペイロードを実行して `out.txt` を作る |
| `python agent.py --clip` | `in.md` の代わりにクリップボードから読む |
| `python agent.py resume` | 新チャット用の引き継ぎパケット `resume.txt` を作る |

## 覚えておく合図

- **`verify GREEN / RED`**（コンソール） — 進捗の真実はモデルの申告ではなくここ
- **`ASK -- the agent needs YOUR decision`** — あなた（依頼主）への質問。
  out.txt は作られない。チャットに直接、短く答えて
  「プロトコルに戻り、次のペイロードを出して」と添える
- **`PATCH FAILED` / `no FILE section` 等** — そのまま貼り戻せばモデルが自己修正する
- **普通のチャット口調で「このコードを貼って」と言い出した** — プロトコル脱落。
  手で編集せず、RUNBOOK のトラブル対応にある戻し文句を送る。
  再発するならチャットの世代交代（resume）

詳細・トラブル対応は [RUNBOOK.md](RUNBOOK.md) へ。
