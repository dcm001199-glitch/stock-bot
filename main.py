import os
import asyncio
import sqlite3
from datetime import datetime
import zoneinfo

from aiogram import Bot, Dispatcher, types, executor
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import yfinance as yf
import pandas as pd
import matplotlib.pyplot as plt
from io import BytesIO

TOKEN = os.getenv("TOKEN")
bot = Bot(token=TOKEN, parse_mode="HTML")
dp = Dispatcher(bot)

# 数据库（全局连接，Railway 支持持久化）
conn = sqlite3.connect('stocks.db', check_same_thread=False)
conn.execute('''CREATE TABLE IF NOT EXISTS watches 
             (user_id INTEGER, symbol TEXT, target REAL, type TEXT)''')
conn.commit()

# K线图函数
def plot_kline(symbol: str, period: str = "10d"):
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period)
        if df.empty: return None

        plt.style.use('dark_background')
        fig, ax = plt.subplots(figsize=(10, 6), dpi=150)
        colors = ['green' if c >= o else 'red' for c, o in zip(df['Close'], df['Open'])]
        ax.bar(df.index, df['Close'] - df['Open'], bottom=df['Open'], color=colors, width=0.8)
        ax.bar(df.index, df['High'] - df['Low'], bottom=df['Low'], color=colors, width=0.15)
        df['MA5'] = df['Close'].rolling(5).mean()
        df['MA20'] = df['Close'].rolling(20).mean()
        ax.plot(df.index, df['MA5'], color='#FFA500', label='MA5', linewidth=1.3)
        ax.plot(df.index, df['MA20'], color='#00BFFF', label='MA20', linewidth=1.3)
        ax.set_title(f"{symbol}  当前价: {df['Close'].iloc[-1]:.2f}", color='white', fontsize=16)
        ax.legend(); ax.grid(alpha=0.3)

        buf = BytesIO()
        plt.savefig(buf, format='png', bbox_inches='tight')
        buf.seek(0); plt.close()
        return buf
    except: return None

# start
@dp.message_handler(commands=['start'])
async def start(m): 
    kb = InlineKeyboardMarkup().add(InlineKeyboardButton("使用说明", callback_data="help"))
    await m.answer("🚀 你的私人股票监控机器人已上线！\n直接发股票代码即可查价+看K线", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data == "help")
async def help_cb(call):
    await call.message.edit_text(
        "<b>使用说明：</b>\n\n"
        "直接发代码：AAPL / 00700.HK / 000001.SH\n\n"
        "/add AAPL 180 上 → 涨破180提醒\n"
        "/add 600519.SH 5% 下 → 跌超5%提醒\n\n"
        "/list /del 3 /clear", parse_mode="HTML")

# 查询价格+发图
@dp.message_handler(regexp=r'^[A-Z0-9\.\-]{2,12}$')
async def price(m):
    symbol = m.text.strip().upper()
    ticker = yf.Ticker(symbol)
    hist = ticker.history(period="2d")
    info = ticker.info
    if hist.empty:
        await m.reply("❌ 无效代码")
        return
    close = hist['Close'][-1]
    pre = hist['Close'][-2] if len(hist)>1 else close
    change = close - pre
    pct = change/pre*100 if pre else 0
    name = info.get('longName') or info.get('shortName') or symbol
    text = f"<b>{name}</b> ({symbol})\n现价: <b>{close:.2f}</b>  涨跌: {change:+.2f} ({pct:+.2f}%)\n时间: {datetime.now(zoneinfo.ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M')}"
    buf = plot_kline(symbol)
    if buf: await m.reply_photo(buf, caption=text)
    else: await m.reply(text)

# 添加监控
@dp.message_handler(commands=['add'])
async def add(m):
    try:
        p = m.text.split()[1:]
        sym, tar, dir_ = p[0].upper(), p[1], p[2]
        if dir_ not in ['上','下']: raise
        if tar.endswith('%'):
            val = float(tar[:-1])
            typ = 'pct_up' if dir_=='上' else 'pct_down'
        else:
            val = float(tar)
            typ = 'price_up' if dir_=='上' else 'price_down'
        conn.execute("INSERT INTO watches VALUES (?,?,?,?)", (m.from_user.id, sym, val, typ))
        conn.commit()
        await m.reply(f"✅ 已添加：{sym} {tar}{dir_}破提醒")
    except:
        await m.reply("格式错！示例：\n/add AAPL 180 上\n/add 000001.SH 6% 下")

# 其他命令（list del clear）
@dp.message_handler(commands=['list'])
async def list_(m):
    cur = conn.execute("SELECT rowid,* FROM watches WHERE user_id=?", (m.from_user.id,))
    rows = cur.fetchall()
    if not rows: await m.reply("空空如也"); return
    txt = "<b>监控列表：</b>\n\n"
    for r in rows:
        if 'pct' in r[4]: txt += f"{r[0]}. {r[2]} 今日{'涨超' if 'up' in r[4] else '跌超'} <b>{r[3]}%</b>\n"
        else: txt += f"{r[0]}. {r[2]} {'涨破' if 'up' in r[4] else '跌破'} <b>{r[3]}</b>\n"
    await m.reply(txt)

@dp.message_handler(commands=['del'])
async def dele(m):
    try:
        idx = int(m.text.split()[1])
        conn.execute("DELETE FROM watches WHERE rowid=? AND user_id=?", (idx, m.from_user.id))
        conn.commit(); await m.reply("✅ 已删除")
    except: await m.reply("用法：/del 3")

@dp.message_handler(commands=['clear'])
async def clear(m):
    conn.execute("DELETE FROM watches WHERE user_id=?", (m.from_user.id,))
    conn.commit(); await m.reply("🗑 已清空")

# 后台监控任务
async def checker():
    while True:
        cur = conn.execute("SELECT rowid,user_id,symbol,target,type FROM watches")
        for row in cur.fetchall():
            rid,uid,sym,tar,typ = row
            try:
                h = yf.Ticker(sym).history(period="2d")
                if len(h)<2: continue
                close, pre = h['Close'][-1], h['Close'][-2]
                pct = (close-pre)/pre*100
                msg = ""
                if typ=='price_up' and close>=tar: msg = f"🚀 {sym} 已涨破 {tar}\n现价 {close:.2f}"
                elif typ=='price_down' and close<=tar: msg = f"💥 {sym} 已跌破 {tar}\n现价 {close:.2f}"
                elif typ=='pct_up' and pct>=tar: msg = f"🟢 {sym} 今日涨超 {tar}%\n当前 {pct:+.2f}%"
                elif typ=='pct_down' and pct<=-tar: msg = f"🔴 {sym} 今日跌超 {tar}%\n当前 {pct:+.2f}%"
                if msg:
                    await bot.send_message(uid, msg)
                    conn.execute("DELETE FROM watches WHERE rowid=?", (rid,))
            except: pass
        conn.commit()
        await asyncio.sleep(45)

if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    loop.create_task(checker())
    executor.start_polling(dp, skip_updates=True)
