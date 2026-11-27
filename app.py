import os
import uuid
from datetime import date, datetime, timedelta, timezone

from flask import (
    Flask, render_template, request, redirect, url_for, flash, session, abort, Response,
)
from werkzeug.security import generate_password_hash, check_password_hash
from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableServiceClient, UpdateMode
from azure.storage.blob import BlobServiceClient, ContentSettings

app = Flask(__name__)
# セッション（「わたしも知りたい」の二重押し防止）用。本番では App Service のアプリ設定で SECRET_KEY を設定する
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-secret")
# アップロードは 5MB まで
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

# 環境変数から接続文字列を読み込む（コードには書かない）
CONNECTION_STRING = os.environ["AZURE_STORAGE_CONNECTION_STRING"]

table_service = TableServiceClient.from_connection_string(CONNECTION_STRING)
questions = table_service.create_table_if_not_exists("questions")
answers = table_service.create_table_if_not_exists("answers")

blob_service = BlobServiceClient.from_connection_string(CONNECTION_STRING)
images = blob_service.get_container_client("images")
try:
    images.create_container()  # 非公開コンテナー。画像は /image/<名前> 経由で返す
except Exception:
    pass  # すでにある

JST = timezone(timedelta(hours=9))
SUBJECTS = ["国語", "数学", "英語", "物理", "化学", "生物", "日本史", "世界史", "地理", "政治経済", "情報", "その他"]
ALLOWED_IMAGES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp",
}
NG_WORDS = ["死ね", "殺す", "きもい", "うざい", "ブス"]
QUESTION_PK = "q"


def now_iso():
    return datetime.now(JST).isoformat()


def today():
    return datetime.now(JST).date()


def has_ng_word(*texts):
    return any(ng in t for t in texts for ng in NG_WORDS)


def save_image(file):
    """アップロードされた画像を Blob に保存し、Blob 名を返す。画像がなければ ''"""
    if not file or not file.filename:
        return ""
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_IMAGES:
        raise ValueError("画像は jpg / png / gif / webp だけです")
    name = f"{uuid.uuid4()}{ext}"
    images.upload_blob(
        name, file.stream,
        content_settings=ContentSettings(content_type=ALLOWED_IMAGES[ext]),
    )
    return name


def days_left(exam_date):
    if not exam_date:
        return None
    return (date.fromisoformat(exam_date) - today()).days


def get_question(qid):
    try:
        return questions.get_entity(partition_key=QUESTION_PK, row_key=qid)
    except ResourceNotFoundError:
        abort(404)


@app.route("/")
def index():
    subject = request.args.get("subject", "")
    sort = request.args.get("sort", "new")
    only_open = request.args.get("open") == "1"

    items = list(questions.query_entities("PartitionKey eq @pk", parameters={"pk": QUESTION_PK}))
    if subject in SUBJECTS:
        items = [q for q in items if q["subject"] == subject]
    if only_open:
        items = [q for q in items if not q["solved"]]

    if sort == "metoo":
        items.sort(key=lambda q: (q["metoo"], q["created_at"]), reverse=True)
    elif sort == "exam":
        # テストが近い順（日付未設定・過去のテストは後ろ）
        def exam_key(q):
            d = days_left(q.get("exam_date"))
            return (d is None or d < 0, d if d is not None else 0)
        items.sort(key=exam_key)
    else:
        items.sort(key=lambda q: q["created_at"], reverse=True)

    for q in items:
        q["days_left"] = days_left(q.get("exam_date"))

    return render_template(
        "index.html",
        items=items, subjects=SUBJECTS, subject=subject, sort=sort, only_open=only_open,
        metoo_done=session.get("metoo", []),
    )


@app.route("/ask", methods=["POST"])
def ask():
    subject = request.form.get("subject", "")
    title = request.form.get("title", "").strip()[:60]
    body = request.form.get("body", "").strip()[:2000]
    nickname = request.form.get("nickname", "").strip()[:20] or "名無しの受験生"
    exam_date = request.form.get("exam_date", "")
    passcode = request.form.get("passcode", "")

    if subject not in SUBJECTS:
        flash("科目を選んでください")
        return redirect(url_for("index"))
    if not title or not body:
        flash("タイトルと質問内容を入力してください")
        return redirect(url_for("index"))
    if not (4 <= len(passcode) <= 20):
        flash("合言葉は 4〜20 文字で入力してください")
        return redirect(url_for("index"))
    if exam_date:
        try:
            date.fromisoformat(exam_date)
        except ValueError:
            exam_date = ""
    if has_ng_word(title, body, nickname):
        flash("人を傷つける言葉が含まれているため投稿できません")
        return redirect(url_for("index"))
    try:
        image = save_image(request.files.get("image"))
    except ValueError as e:
        flash(str(e))
        return redirect(url_for("index"))

    qid = str(uuid.uuid4())
    questions.create_entity({
        "PartitionKey": QUESTION_PK,
        "RowKey": qid,
        "subject": subject,
        "title": title,
        "body": body,
        "nickname": nickname,
        "exam_date": exam_date,
        "image": image,
        "metoo": 0,
        "answer_count": 0,
        "solved": False,
        "passcode_hash": generate_password_hash(passcode),
        "created_at": now_iso(),
    })
    flash("質問を投稿しました。合言葉は「解決済み」にするときに使います")
    return redirect(url_for("question", qid=qid))


@app.route("/q/<qid>")
def question(qid):
    q = get_question(qid)
    q["days_left"] = days_left(q.get("exam_date"))
    ans = list(answers.query_entities("PartitionKey eq @pk", parameters={"pk": qid}))
    ans.sort(key=lambda a: a["created_at"])
    return render_template(
        "question.html", q=q, answers=ans, metoo_done=session.get("metoo", []),
    )


@app.route("/q/<qid>/answer", methods=["POST"])
def answer(qid):
    q = get_question(qid)
    body = request.form.get("body", "").strip()[:2000]
    nickname = request.form.get("nickname", "").strip()[:20] or "名無しの先輩"

    if not body:
        flash("回答を入力してください")
        return redirect(url_for("question", qid=qid))
    if has_ng_word(body, nickname):
        flash("人を傷つける言葉が含まれているため投稿できません")
        return redirect(url_for("question", qid=qid))
    try:
        image = save_image(request.files.get("image"))
    except ValueError as e:
        flash(str(e))
        return redirect(url_for("question", qid=qid))

    answers.create_entity({
        "PartitionKey": qid,
        "RowKey": str(uuid.uuid4()),
        "body": body,
        "nickname": nickname,
        "image": image,
        "created_at": now_iso(),
    })
    q["answer_count"] = q.get("answer_count", 0) + 1
    questions.update_entity(q, mode=UpdateMode.MERGE)
    flash("回答しました。ありがとう！")
    return redirect(url_for("question", qid=qid))


@app.route("/q/<qid>/metoo", methods=["POST"])
def metoo(qid):
    done = session.get("metoo", [])
    if qid not in done:
        q = get_question(qid)
        q["metoo"] = q.get("metoo", 0) + 1
        questions.update_entity(q, mode=UpdateMode.MERGE)
        session["metoo"] = (done + [qid])[-200:]
    return redirect(request.form.get("next") or url_for("question", qid=qid))


@app.route("/q/<qid>/solve", methods=["POST"])
def solve(qid):
    q = get_question(qid)
    if not check_password_hash(q["passcode_hash"], request.form.get("passcode", "")):
        flash("合言葉がちがいます")
        return redirect(url_for("question", qid=qid))
    q["solved"] = True
    questions.update_entity(q, mode=UpdateMode.MERGE)
    flash("解決済みにしました！")
    return redirect(url_for("question", qid=qid))


@app.route("/image/<name>")
def image(name):
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED_IMAGES:
        abort(404)
    try:
        data = images.download_blob(name).readall()
    except ResourceNotFoundError:
        abort(404)
    return Response(
        data, mimetype=ALLOWED_IMAGES[ext],
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.errorhandler(413)
def too_large(_):
    flash("画像は 5MB までです")
    return redirect(request.referrer or url_for("index"))


if __name__ == "__main__":
    app.run(debug=True)
