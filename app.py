import os
import uuid
from datetime import date, datetime, timedelta, timezone

from flask import Flask, render_template, request, redirect, url_for, flash
from azure.data.tables import TableServiceClient

app = Flask(__name__)
# flash メッセージ用。本番では App Service のアプリ設定で SECRET_KEY を設定する
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-secret")

# 環境変数から接続文字列を読み込む（コードには書かない）
CONNECTION_STRING = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
TABLE_NAME = "expenses"

service = TableServiceClient.from_connection_string(CONNECTION_STRING)
table = service.create_table_if_not_exists(TABLE_NAME)

JST = timezone(timedelta(hours=9))
CATEGORIES = ["食費", "交通費", "日用品", "趣味", "交際費", "その他"]


def this_month():
    return datetime.now(JST).strftime("%Y-%m")


def shift_month(month, delta):
    """'2026-11' と +1/-1 を受け取り、前後の月を返す"""
    y, m = map(int, month.split("-"))
    m += delta
    if m == 0:
        y, m = y - 1, 12
    elif m == 13:
        y, m = y + 1, 1
    return f"{y:04d}-{m:02d}"


def valid_month(value):
    try:
        datetime.strptime(value, "%Y-%m")
        return True
    except (TypeError, ValueError):
        return False


@app.route("/")
def index():
    month = request.args.get("month", this_month())
    if not valid_month(month):
        month = this_month()

    # PartitionKey = 月（例: 2026-11）なので、その月のデータだけを取り出せる
    items = list(table.query_entities("PartitionKey eq @pk", parameters={"pk": month}))
    items.sort(key=lambda e: (e["date"], e["created_at"]), reverse=True)

    total = sum(e["amount"] for e in items)

    # カテゴリ別の合計（多い順）
    by_category = {}
    for e in items:
        by_category[e["category"]] = by_category.get(e["category"], 0) + e["amount"]
    breakdown = sorted(by_category.items(), key=lambda kv: kv[1], reverse=True)

    return render_template(
        "index.html",
        month=month,
        prev_month=shift_month(month, -1),
        next_month=shift_month(month, 1),
        items=items,
        total=total,
        breakdown=breakdown,
        categories=CATEGORIES,
        today=datetime.now(JST).strftime("%Y-%m-%d"),
    )


@app.route("/add", methods=["POST"])
def add():
    spent_on = request.form.get("date", "")
    category = request.form.get("category", "")
    amount_text = request.form.get("amount", "").strip()
    memo = request.form.get("memo", "").strip()[:100]

    # 入力チェック
    try:
        date.fromisoformat(spent_on)
    except ValueError:
        flash("日付が正しくありません")
        return redirect(url_for("index"))
    if category not in CATEGORIES:
        flash("カテゴリを選んでください")
        return redirect(url_for("index", month=spent_on[:7]))
    if not amount_text.isdigit() or not (0 < int(amount_text) <= 10_000_000):
        flash("金額は 1〜10,000,000 の整数で入力してください")
        return redirect(url_for("index", month=spent_on[:7]))

    month = spent_on[:7]
    table.create_entity({
        "PartitionKey": month,
        "RowKey": str(uuid.uuid4()),
        "date": spent_on,
        "category": category,
        "amount": int(amount_text),
        "memo": memo,
        "created_at": datetime.now(JST).isoformat(),
    })
    flash(f"{category} {int(amount_text):,}円 を追加しました")
    return redirect(url_for("index", month=month))


@app.route("/delete", methods=["POST"])
def delete():
    month = request.form.get("month", "")
    row_key = request.form.get("row_key", "")
    if valid_month(month) and row_key:
        table.delete_entity(partition_key=month, row_key=row_key)
        flash("削除しました")
    return redirect(url_for("index", month=month if valid_month(month) else None))


if __name__ == "__main__":
    app.run(debug=True)
