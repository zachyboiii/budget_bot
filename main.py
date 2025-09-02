import os
import logging
import pandas as pd
from datetime import datetime
import calendar
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ------------------- Setup -------------------
if os.path.exists(".env"):
    load_dotenv()  # only load locally

TOKEN = os.getenv("TELEGRAM_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")

client = MongoClient(
    MONGO_URI,
    tls=True,
    tlsAllowInvalidCertificates=False
)
db = client["budgetbot"]
users = db["users"]
budgets = db["budgets"]
expenses = db["expenses"]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)


# ------------------- Helpers -------------------
def get_month_str(date=None):
    """Return YYYY-MM string for current (or given) date."""
    if not date:
        date = datetime.utcnow()
    return date.strftime("%Y-%m")

def get_month_range(month_str):
    """Return the first and last datetime of a given YYYY-MM month."""
    year, month = map(int, month_str.split("-"))
    first_day = datetime(year, month, 1)
    last_day = datetime(year, month, calendar.monthrange(year, month)[1])
    return first_day, last_day


# ------------------- Commands -------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or f"user_{user_id}"

    # Check if user exists
    user_doc = users.find_one({"uid": user_id})

    if not user_doc:
        # Add new user to database
        users.insert_one({
            "uid": user_id,
            "username": username,
            "budget": {},        # placeholder for budget data
            "expenses": []       # list of expense references
        })
        welcome_msg = f"👋 Welcome, {username}! Your account has been created.\n\n"
    else:
        welcome_msg = f"👋 Welcome back, {username}!\n\n"

    # Send the commands help message
    welcome_msg += (
        "Commands:\n"
        "/setbudget <amount>\n"
        "/add <amount> <name> <category>\n"
        "/balance\n"
        "/view <YYYY-MM>\n"
        "/export <YYYY-MM>\n"
        "/help\n"
    )

    await update.message.reply_text(welcome_msg)


async def set_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /setbudget <amount>")
        return

    user_id = update.effective_user.id
    username = update.effective_user.username or f"user_{user_id}"
    month = get_month_str()

    budget_doc = {
        "uid": user_id,
        "username": username,
        "month": month,
        "budget": amount,
        "created_at": datetime.utcnow(),
    }

    budgets.update_one(
        {"uid": user_id, "month": month},
        {"$set": budget_doc},
        upsert=True
    )

    # update user doc’s budget
    users.update_one({"uid": user_id}, {"$set": {"budget": budget_doc}})

    await update.message.reply_text(f"✅ Budget for {month} set to {amount:.2f}")


async def add_expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or f"user_{user_id}"
    now = datetime.utcnow()

    try:
        # Get the text after /add
        text = update.message.text[len("/add"):].strip()
        # Split by comma into amount, name, category
        amount_str, name, category = [x.strip() for x in text.split(",", 2)]
        amount = float(amount_str)
    except Exception:
        await update.message.reply_text(
            "Usage: /add <amount>, <name>, <category>\nExample: /add 12.50, Lunch at, Food"
        )
        return

    expense_doc = {
        "uid": user_id,
        "username": username,
        "amount": amount,
        "name": name,
        "category": category,
        "timestamp": now,
    }

    expenses.insert_one(expense_doc)

    # Update user’s expenses list
    users.update_one(
        {"uid": user_id},
        {"$push": {"expenses": {"amount": amount, "name": name, "category": category, "timestamp": now}}}
    )

    reply = f"Successfully added expense! \nAmount: {amount:.2f}\nName: {name}\nCategory: {category}"

    await update.message.reply_text(reply)



async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    month = get_month_str()

    budget_doc = budgets.find_one({"uid": user_id, "month": month})
    if not budget_doc:
        await update.message.reply_text("⚠️ No budget set for this month.")
        return
    start, end = get_month_range(month)

    pipeline = [
        {"$match": {
            "uid": user_id,
            "timestamp": {
                "$gte": start,
                "$lt": end,
            }
        }},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
    ]

    expense_total = 0
    for result in expenses.aggregate(pipeline):
        expense_total = result["total"]

    balance = budget_doc["budget"] - expense_total
    await update.message.reply_text(
        "Balance Summary for the month:\n\n"
        f"💰 Budget: {budget_doc['budget']:.2f}\n"
        f"📉 Spent: {expense_total:.2f}\n"
        f"✅ Balance: {balance:.2f}"
    )


async def view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        month = context.args[0]
        datetime.strptime(month, "%Y-%m")  # validate format
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /view <YYYY-MM>")
        return

    user_id = update.effective_user.id
    start, end = get_month_range(month)

    cursor = expenses.find({
        "uid": user_id,
        "timestamp": {
            "$gte": start,
            "$lt": end,
        }
    })

    exp_list = list(cursor)
    if not exp_list:
        await update.message.reply_text(f"No expenses found for {month}.")
        return

    msg = f"📒 Expenses for {month}:\n\n"
    total = 0
    for exp in exp_list:
        amt = exp["amount"]
        name = exp.get("name", "N/A")
        cat = exp.get("category", "Uncategorized")
        ts = exp["timestamp"].strftime("%Y-%m-%d")
        msg += f"- {ts}: {amt:.2f} [{name}] ({cat})\n"
        total += amt

    msg += f"\nTotal: {total:.2f}"
    await update.message.reply_text(msg)


async def export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        month = context.args[0]
        datetime.strptime(month, "%Y-%m")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /export <YYYY-MM>")
        return

    user_id = update.effective_user.id
    start, end = get_month_range(month)

    cursor = expenses.find({
        "uid": user_id,
        "timestamp": {
            "$gte": start,
            "$lt": end,
        }
    })

    exp_list = list(cursor)
    if not exp_list:
        await update.message.reply_text(f"No expenses to export for {month}.")
        return

    # Convert to DataFrame
    df = pd.DataFrame(exp_list)
    df.drop(columns=["_id"], inplace=True, errors="ignore")

    filename = f"expenses_{user_id}_{month}.csv"
    df.to_csv(filename, index=False)

    await update.message.reply_document(document=open(filename, "rb"))
    os.remove(filename)

async def help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_msg = (
        "Commands:\n\n"
        "/setbudget <amount> - Set budget for the month\n"
        "/add <amount>, <name>, <category> - Add an expense with the given format\n"
        "/balance - View balance for the month\n"
        "/view <YYYY-MM> - View expenses for the specified month\n"
        "/export <YYYY-MM> - Export expenses for the specified month as a CSV file\n"
    )
    await update.message.reply_text(help_msg)


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    not_found_msg = (
        "⚠️ Command not found ⚠️ \n\n"
        "Available Commands:\n"
        "/setbudget <amount>\n"
        "/add <amount>, <name>, <category>\n"
        "/balance\n"
        "/view <YYYY-MM>\n"
        "/export <YYYY-MM>\n"
        "/help\n"
    )
    await update.message.reply_text(not_found_msg)


# ------------------- Main -------------------
def main():
    print(TOKEN)
    print(MONGO_URI)
    app = Application.builder().token(TOKEN).build()
    print("Bot started...")
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        client.server_info()  # forces a connection attempt
        print("✅ Connected to MongoDB successfully!")
    except ServerSelectionTimeoutError as e:
        print("❌ Could not connect to MongoDB:", e)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setbudget", set_budget))
    app.add_handler(CommandHandler("add", add_expense))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("view", view))
    app.add_handler(CommandHandler("export", export))
    app.add_handler(CommandHandler("help", help))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    app.run_polling()


if __name__ == "__main__":
    main()
