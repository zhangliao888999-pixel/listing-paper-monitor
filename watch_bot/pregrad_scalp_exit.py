# -*- coding: utf-8 -*-
"""2026-07-29新增: "毕业前抢筹,毕业前卖回curve"打法的纸盘执行器——跟snipe_exit.py
是两套完全不同的退出逻辑,不能共用:

  snipe_exit.py 的策略是"买入已毕业/接近毕业的池子,等真买家/操盘方卖出信号才跑",
  会持仓跨越毕业这个瞬间,吃的是毕业后新池子里的行情。

  这个脚本反过来: 只在老池子(bonding curve,毕业前)里买卖,全程不碰新池子。
  用户的原话是"毕业后1秒马上跑",但REDO/FRANK两个案例分析发现毕业瞬间到底有没有
  反应窗口是抛硬币(REDO有52秒窗口,FRANK毕业跟砸盘是同一秒),跟操盘方自己的
  砸盘交易抢同一个身位赢面很小。所以改成更稳的版本: 只吃bonding curve内部本身
  的涨幅(REDO这段实测涨了993%,比毕业瞬间那47%跳涨大得多),用移动止盈+硬止损+
  硬超时控制风险,一旦观察到"这个池子好像要没了"(有可能是要毕业了,也可能是真的
  死了),不管是哪种都不恋战,直接按当前价卖出离场——不去赌毕业后的行情。

跟snipe_exit.py一样是纯DRY-RUN、写同一份journal.jsonl(found_via标记为
pregrad_ramp,方便以后跟其他策略横向对比)。这个阶段的代币在Jupiter上大概率还
查不到报价(bonding curve没接入聚合器路由,这也是"毕业后大家才能买"这件事本身
的技术原因),所以不像snipe_exit.py那样调用Jupiter模拟买卖行情,直接用
GeckoTerminal自己报的价格做纸面结算——反正只是纸盘,不需要真实可执行的报价。

用法: python pregrad_scalp_exit.py <池子地址> <mint> <文件前缀>

2026-07-29白天补充: 用户看完统计后明确要求补一条"狗庄毕业币纸盘模拟买入"——
既然17个已核实毕业样本里外部资金17/17全部净亏,这条腿定位是纯数据采集/验证,
不是"找到了新的赚钱思路"。检测到大概率已毕业(连续查不到老池子)时,直接拉起
post_grad_scalp_exit.py接手去查新池子、用更紧的风控参数单独跑一笔纸盘,数据
写进同一份journal.jsonl(found_via=post_grad_scalp),不阻塞这个脚本自己收尾退出。
"""
import base64
import json
import os
import re
import subprocess
import sys
import time
import datetime as dt
from pathlib import Path

import requests

from git_lock import git_lock, resolve_stuck_merge, run_git, GIT_PULL_CMD

HERE = Path(__file__).parent
JOURNAL_F = HERE / "journal.jsonl"

# 2026-07-31新增: pregrad上实盘。用户用数据拍板: 纸盘里pregrad是565笔/天,
# snipe腿只有35笔/天且大半是秒进秒出的0%记录,要快速攒20-30笔实盘样本只能
# 靠这条腿。毕业前的币不在Jupiter路由里(这正是"毕业"的技术含义),实盘走
# PumpPortal的trade-local接口: 它只负责构造未签名交易,签名始终在本地用
# WALLET_PRIVATE_KEY完成,私钥不外传——跟snipe_exit.py的Jupiter流程同一个
# 安全模型。代价: 每笔0.5%服务费+交易内容由第三方构造(钱包只放小额)。
PUMPPORTAL_URL = "https://pumpportal.fun/api/trade-local"
SLIPPAGE_PCT = float(os.environ.get("PUMP_SLIPPAGE_PCT", "10"))
PRIORITY_FEE_SOL = float(os.environ.get("PUMP_PRIORITY_FEE_SOL", "0.0005"))
SOL_MINT = "So11111111111111111111111111111111111111112"
# 2026-07-31新增: 按发射台分流执行通道。分台统计发现纸盘893笔pregrad里
# pump.fun占49%(均值-2.7%)、Meteora DBC系占49%(均值+13.6%),利润几乎全在
# 后者——只做pump.fun等于专挑亏钱的那一半。PumpPortal不支持Meteora DBC,
# 但实测Jupiter能路由它们的曲线池(CT/Fraggle实测路由=Meteora DAMM v2),
# 所以: pump/bonk后缀走PumpPortal,其余走Jupiter,复用同一套本地签名流程。
JUPITER_QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL = "https://lite-api.jup.ag/swap/v1/swap"
JUP_SLIPPAGE_BPS = int(os.environ.get("SLIPPAGE_BPS", "300"))


def venue_for(mint):
    """哪条执行通道能做这个币: PumpPortal只支持pump.fun/letsbonk系,
    Meteora DBC等其余发射台走Jupiter聚合器路由。"""
    m = str(mint)
    return "pumpportal" if (m.endswith("pump") or m.endswith("bonk")) else "jupiter"
LIVE_POSITION_MARKER = HERE / ".live_position_open"  # 跟snipe_exit.py共用同一个
                                                      # 全局实盘名额标记文件


def is_live_mode():
    return os.environ.get("LIVE_TRADING") == "1" and os.environ.get("CONFIRM_LIVE_SNIPE") == "YES"

GT_BASE = "https://api.geckoterminal.com/api/v2"
S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                  "Accept": "application/json;version=20230302"})

POS_SIZE_USD = float(os.environ.get("POS_SIZE_USD", "5.0"))
# 2026-07-29白天先调紧、当晚发现问题又调回来: 419笔回看后把POLL_SEC从5秒收到3秒+
# 1.5秒自适应加密复查,想缩小止损超调,但上线后云端43分钟0笔交易完成——GT接口本身
# 响应正常(0.1-0.4秒),真正原因是pregrad_scanner.py当时没有并发上限,好几个仓位
# 同时用更快频率轮询,自己把请求量顶到限流,越忙越忙不过来。现在pregrad_scanner.py
# 那边加了MAX_CONCURRENT=6的硬上限,这边轮询间隔改回5秒、去掉自适应加密逻辑——
# 双管齐下先把吞吐量救回来,止损超调的问题留到并发和限流都稳定之后再单独调。
POLL_SEC = 5
FAST_RECHECK_SEC = POLL_SEC  # 暂时跟POLL_SEC一致,相当于关闭加密复查,先保吞吐量
MAX_HOLD_SEC = 180        # 硬超时3分钟——REDO/FRANK从创世到毕业都在这个量级内
# 2026-07-29晚间撤回: v3的90秒"无动能提前离场"检测,用户查了JESTERS这一笔实锤——
# 91秒内价格深V(暴跌到entry的24%又拉回接近原价),净变动看起来~0%触发提前离场,
# 但退场后20-30分钟price从1.03e-5一路拉到1.94e-5(+412% h6)。90秒的净变动量
# 分不清"真的没动能"和"剧烈整理蓄势",而这个策略的收益完全靠偶尔抓到这种肥尾撑
# 起来(v1数据: HARD_TIMEOUT中位数~0%,均值+27%全靠极少数暴力单)——错杀一个
# 肥尾的机会成本,远大于另外4/5正确案例省下的那点"本来就快到0%"的亏损。整条
# 规则移除,不再提前离场,只靠移动止盈/硬止损/硬超时三条线控制风险。
PLATEAU_CHECK_SEC = None
# 原20%/35%的止损止盈线,实测中位数超调都相当可观(尤其HARD_STOP_LOSS超调13.3pp,
# 45%的单子超调>20pp)——既然结算价本来就会比设定线更差,把设定线本身收紧,
# 让"更差的结算价"落在更能接受的范围,而不是继续放任-48%中位数这种结果。
TRAIL_STOP_PCT = 15       # 从最高点回撤这么多就跑,不贪图猜中最高点(原20)
HARD_STOP_LOSS_PCT = 20   # 从没盈利过、直接跌破入场价这么多,说明这次没被拉起来,止损离场(原35)

# 2026-07-29白天新增: 用户明确要求把参数改动前后的数据分开标记,不然新旧参数
# 混在一起没法判断这次优化到底有没有效果。v1=改动前(5秒轮询,20%/35%止盈止损,
# 无流动性门槛,无90秒无动能提前离场);v2=本次改动后(见上面几个常量)。
# journal.jsonl里v1时期的历史记录已经用一次性迁移脚本补上了这个字段。
STRATEGY_VERSION = 5   # v2=半途夭折的坏版本(3秒轮询无并发上限,云端卡死);
                        # v3=轮询改回5秒+并发上限+90秒无动能提前离场;
                        # v4=撤回90秒提前离场(JESTERS实锤证明会错杀肥尾),
                        # 只保留移动止盈15%/硬止损20%/流动性入场门槛这几项改动;
                        # v5=v4跑了97笔后回报率持续走低,按入场流动性分桶发现
                        # $0-10K区间(占了一半以上样本)均值是负的,$10K+才转正,
                        # 把matches_pregrad_ramp_signature的流动性门槛从$3000
                        # 提到$10000(operator_registry.py),这里同步升版本号

# 2026-07-30新增: 云端(GitHub Actions)和VPS现在推送的是同一个仓库同一份
# journal.jsonl,数据混在一起分不清是哪边跑出来的,没法对比两边的效率/质量。
# 加一个环境变量标记来源,GitHub Actions workflow里设DEPLOY_ENV=github_actions,
# VPS的vps_run_forever.ps1里设DEPLOY_ENV=vps,本地跑不设时默认local。
DEPLOY_ENV = os.environ.get("DEPLOY_ENV", "local")

LOG_F = None


def log(msg):
    ts = dt.datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        print(line)
    except UnicodeEncodeError:
        pass
    with LOG_F.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def get(url, params=None, tries=3):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(2 * (i + 1))
                continue
            return None
        except requests.RequestException:
            time.sleep(1.5 * (i + 1))
    return None


def get_wallet():
    """跟snipe_exit.py同一套安全边界: 私钥只从环境变量读进内存,从不写日志/落盘/
    随网络请求外传。没设就直接退出,绝不静默降级成纸盘假装在跑实盘。"""
    pk = os.environ.get("WALLET_PRIVATE_KEY")
    if not pk:
        log("FATAL: 环境变量 WALLET_PRIVATE_KEY 未设置,本机不持有私钥无法下单")
        sys.exit(1)
    try:
        from solders.keypair import Keypair
        return Keypair.from_base58_string(pk)
    except Exception:
        log("FATAL: WALLET_PRIVATE_KEY 格式不对(需要base58编码的私钥字符串)")
        sys.exit(1)


def get_sol_price_usd():
    d = get(f"{GT_BASE}/networks/solana/tokens/{SOL_MINT}")
    try:
        return float(d["data"]["attributes"]["price_usd"])
    except (KeyError, TypeError, ValueError):
        return None


def rpc_call(method, params):
    try:
        r = requests.post("https://api.mainnet-beta.solana.com",
                          json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        return {"error": str(e)}


def get_wallet_lamports(pubkey_str):
    resp = rpc_call("getBalance", [pubkey_str])
    try:
        return int(resp["result"]["value"])
    except (KeyError, TypeError, ValueError):
        return None


def sign_and_send(wallet, raw_tx_bytes):
    try:
        from solders.transaction import VersionedTransaction
        tx = VersionedTransaction.from_bytes(raw_tx_bytes)
        tx = VersionedTransaction(tx.message, [wallet])
        sig_b64 = base64.b64encode(bytes(tx)).decode()
    except Exception as e:
        return None, str(e)
    resp = rpc_call("sendTransaction", [sig_b64, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}])
    if "error" in resp:
        return None, resp["error"]
    return resp["result"], None


def confirm_tx(sig, tries=15):
    for _ in range(tries):
        resp = rpc_call("getSignatureStatuses", [[sig]])
        try:
            status = resp["result"]["value"][0]
        except (KeyError, IndexError, TypeError):
            status = None
        if status and status.get("confirmationStatus") in ("confirmed", "finalized"):
            return status.get("err") is None
        time.sleep(2)
    return False


def pumpportal_trade(wallet, action, mint, amount, denominated_in_sol, pool):
    """让PumpPortal构造未签名交易 -> 本地签名 -> 自己的RPC广播 -> 等确认。
    返回(tx签名, 是否确认成功, 错误信息)。amount买入时是SOL数量(float),
    卖出时用"100%"字符串一次清仓,不用自己记代币数量。"""
    try:
        r = requests.post(PUMPPORTAL_URL, json={
            "publicKey": str(wallet.pubkey()), "action": action, "mint": mint,
            "amount": amount, "denominatedInSol": "true" if denominated_in_sol else "false",
            "slippage": SLIPPAGE_PCT, "priorityFee": PRIORITY_FEE_SOL, "pool": pool,
        }, timeout=15)
        if r.status_code != 200:
            return None, False, f"PumpPortal HTTP {r.status_code}: {r.text[:150]}"
        raw = r.content
    except requests.RequestException as e:
        return None, False, f"PumpPortal请求失败: {e}"
    sig, err = sign_and_send(wallet, raw)
    if err:
        return None, False, f"广播失败: {err}"
    ok = confirm_tx(sig)
    return sig, ok, None


def jupiter_quote(input_mint, output_mint, amount, slippage_bps):
    try:
        r = requests.get(JUPITER_QUOTE_URL, params={
            "inputMint": input_mint, "outputMint": output_mint,
            "amount": amount, "slippageBps": slippage_bps}, timeout=15)
        return r.json() if r.status_code == 200 else None
    except requests.RequestException:
        return None


def jupiter_swap_tx(quote, user_pubkey):
    try:
        r = requests.post(JUPITER_SWAP_URL, json={
            "quoteResponse": quote, "userPublicKey": user_pubkey, "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True, "prioritizationFeeLamports": "auto"}, timeout=15)
        return r.json().get("swapTransaction") if r.status_code == 200 else None
    except requests.RequestException:
        return None


def get_token_balance_raw(owner_pubkey_str, mint):
    """卖出必须用钱包真实余额,不能用买入报价的预估值(有滑点偏差,会被链上
    模拟以'卖的比持有多'拒掉)——这是snipe腿头两笔卡仓学到的教训。"""
    resp = rpc_call("getTokenAccountsByOwner", [owner_pubkey_str, {"mint": mint}, {"encoding": "jsonParsed"}])
    try:
        accounts = resp["result"]["value"]
        if not accounts:
            return None
        return int(accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def jupiter_trade(wallet, input_mint, output_mint, amount, slippage_bps):
    """Jupiter报价->本地签名->广播->等确认。返回(tx签名, 是否确认, 错误)。"""
    quote = jupiter_quote(input_mint, output_mint, amount, slippage_bps)
    if not quote:
        return None, False, "Jupiter拿不到报价"
    swap_tx = jupiter_swap_tx(quote, str(wallet.pubkey()))
    if not swap_tx:
        return None, False, "Jupiter构造交易失败"
    sig, err = sign_and_send(wallet, base64.b64decode(swap_tx))
    if err:
        return None, False, f"广播失败: {err}"
    return sig, confirm_tx(sig), None


def do_live_buy(wallet, mint, pos_size_usd, sol_price):
    """按发射台分流的实盘买入。返回(tx签名, 是否成功, 错误信息)。"""
    sol_amount = round(pos_size_usd / sol_price, 6)
    if venue_for(mint) == "pumpportal":
        return pumpportal_trade(wallet, "buy", mint, sol_amount, True, "pump")
    lamports = int(sol_amount * 1e9)
    return jupiter_trade(wallet, SOL_MINT, mint, lamports, JUP_SLIPPAGE_BPS)


def do_live_sell(wallet, mint, reason):
    """实盘全仓卖出。pool用auto: 币还在curve里就走pump,已经毕业迁移就自动
    路由到新场地——MISSED_EXIT_LIKELY_GRADUATED这种场景下老curve已关,写死
    pump会卖不掉。失败重试一次(网络抖动),再失败就如实返回,不装成功。"""
    sig = err = None
    if venue_for(mint) == "pumpportal":
        for attempt in (1, 2):
            sig, ok, err = pumpportal_trade(wallet, "sell", mint, "100%", False, "auto")
            if sig and ok:
                log(f"实盘卖出[{reason}]成功 tx={sig}")
                return {"dry_run": False, "confirmed": True, "tx": sig}
            log(f"实盘卖出[{reason}]第{attempt}次失败: {err or '交易未确认'} tx={sig}")
            time.sleep(2)
        return {"dry_run": False, "confirmed": False, "tx": sig, "error": err or "unconfirmed"}

    # Jupiter通道: 用钱包真实余额,滑点逐档放宽——出不来比价格差更糟
    qty = get_token_balance_raw(str(wallet.pubkey()), mint)
    if not qty:
        log(f"实盘卖出[{reason}]失败: 钱包里这个币余额为0")
        return {"dry_run": False, "confirmed": False, "error": "zero_balance"}
    for slippage_bps in (JUP_SLIPPAGE_BPS, 1000, 3000):
        sig, ok, err = jupiter_trade(wallet, mint, SOL_MINT, qty, slippage_bps)
        if sig and ok:
            log(f"实盘卖出[{reason}]成功(滑点容忍{slippage_bps/100:.0f}%) tx={sig}")
            return {"dry_run": False, "confirmed": True, "tx": sig}
        log(f"实盘卖出[{reason}]失败(滑点{slippage_bps/100:.0f}%): {err or '未确认'},换更高滑点重试")
        time.sleep(1.5)
    return {"dry_run": False, "confirmed": False, "tx": sig, "error": err or "unconfirmed"}


def extract_addr(arg):
    m = re.search(r"/pools/([A-Za-z0-9]+)", arg)
    return m.group(1) if m else arg


def check_pool_and_mint(addr):
    d = get(f"{GT_BASE}/networks/solana/pools/{addr}", {"include": "base_token"})
    if not d:
        return None, None
    attrs = d["data"]["attributes"]
    mint = None
    for inc in d.get("included", []):
        if inc.get("type") == "token":
            mint = inc["attributes"].get("address")
    return attrs, mint


def get_pool_snapshot(addr):
    d = get(f"{GT_BASE}/networks/solana/pools/{addr}")
    if not d:
        return None
    a = d.get("data", {}).get("attributes", {})
    try:
        price = float(a.get("base_token_price_usd"))
    except (TypeError, ValueError):
        price = None
    try:
        liq = float(a.get("reserve_in_usd"))
    except (TypeError, ValueError):
        liq = None
    return {"price": price, "liq": liq}


def make_prefix(addr):
    return re.sub(r"[^A-Za-z0-9]", "", addr)[:8]


def handoff_to_post_grad(old_addr, mint, name):
    """检测到大概率已毕业时,拉起独立的post_grad_scalp_exit.py去查新池子、单独
    跑一笔纸盘——用subprocess.Popen不等待,这个脚本自己该收尾就收尾,不因为
    handoff卡住。"""
    try:
        py = sys.executable
        prefix = re.sub(r"[^A-Za-z0-9]", "", mint)[:8]
        kwargs = {"cwd": str(HERE), "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if sys.platform == "win32":
            kwargs["creationflags"] = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                                       | subprocess.CREATE_NO_WINDOW)
        subprocess.Popen([py, str(HERE / "post_grad_scalp_exit.py"), mint, old_addr, prefix], **kwargs)
        log(f"已交接给post_grad_scalp_exit.py去查{name}的新池子")
    except Exception as e:
        log(f"交接post_grad_scalp_exit.py失败: {e}")


def count_prior_entries(mint):
    if not JOURNAL_F.exists():
        return 0
    n = 0
    with JOURNAL_F.open(encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("mint") == mint:
                n += 1
    return n


def write_journal(record):
    record["written_at"] = int(time.time())
    with JOURNAL_F.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def git_push_journal():
    repo_root = HERE.parent
    def run(cmd):
        return run_git(cmd, repo_root)
    # 2026-07-30新增: VPS并发调到6之后好几个进程同时commit+push互相撞车,单靠
    # 重试次数扛不住,加文件锁让这几个脚本的git操作排队,一次只有一个在做。
    with git_lock() as got_lock:
        if not got_lock:
            log("拿不到git锁(30秒超时,可能有很多进程在排队),这次先不推,交给下一轮补推")
            return
        if resolve_stuck_merge(repo_root, log=log):
            log("清理了上一轮遗留的未解决合并冲突")
        run(["git", "add", "watch_bot/journal.jsonl"])
        commit = run(["git", "commit", "-m", f"trade completed: {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}"])
        if commit.returncode != 0 and "nothing to commit" in (commit.stdout + commit.stderr):
            return
        # 2026-07-30再修: 除了VPS内部这4个脚本,本机screener_local和云端GitHub
        # Actions也在并发push同一仓库,锁只挡得住VPS内部互撞,挡不住跨主机的
        # fetch-first冲突。实测3次重试偶尔追不上导致本地积压,加到6次+每次
        # 间隔拉长到5秒,多给一点窗口等其他来源先推完。
        for _ in range(6):
            push = run(["git", "push"])
            if push.returncode == 0:
                log("交易记录已立刻推送到GitHub,页面刷新即可见")
                return
            # 2026-07-30修复: 原来用--rebase,实测VPS并发调高后遇到journal.jsonl这种
            # 只追加的文件冲突,rebase会直接卡住需要人工--abort,普通merge(不加--rebase)
            # 对这类"两边都只是各自追加了新行"的冲突能自动合并,不会卡死。
            run(GIT_PULL_CMD)
            time.sleep(5)
        log("交易记录推送失败(重试6次),会在下一轮lifecycle循环时补推")


def finish_trade(entry_info, exit_reason, exit_price, sell_result=None):
    entry_price = entry_info["entry_price"]
    pnl_pct = (exit_price / entry_price - 1) * 100 if (entry_price and exit_price) else None
    hold_sec = time.time() - entry_info["entry_ts"]
    is_dry = entry_info.get("dry_run", True)

    # 2026-07-31新增: 实盘真实经济账,跟snipe_exit.py同一套字段——pnl_pct是GT
    # 快照的理想价盈亏,pnl_pct_actual才是钱包真实进出的钱,两套并排存,
    # 差值就是滑点+服务费+优先费的真实成本。
    entry_usd_actual = entry_info.get("entry_usd_actual")
    exit_usd_actual = (sell_result or {}).get("exit_usd_actual")
    pnl_pct_actual = None
    if entry_usd_actual and exit_usd_actual is not None:
        pnl_pct_actual = (exit_usd_actual / entry_usd_actual - 1) * 100

    record = {
        "name": entry_info["name"], "mint": entry_info["mint"], "addr": entry_info["addr"],
        "entry_ts": entry_info["entry_ts"], "entry_ts_str": dt.datetime.fromtimestamp(entry_info["entry_ts"]).strftime("%Y-%m-%d %H:%M:%S"),
        "coin_age_min_at_entry": entry_info["coin_age_min_at_entry"],
        "entry_price": entry_price, "entry_liq_usd": entry_info["entry_liq_usd"],
        "entry_locked_liq_pct": entry_info["entry_locked_liq_pct"], "entry_fdv_usd": entry_info["entry_fdv_usd"],
        "n_insiders": None, "found_via": "pregrad_ramp",
        "pos_size_usd": POS_SIZE_USD, "dry_run": is_dry,
        "exit_reason": exit_reason, "exit_price": exit_price, "pnl_pct": pnl_pct,
        "hold_sec": round(hold_sec, 1),
        "prior_entries_on_this_mint": entry_info["prior_entries"],
        "peak_price": entry_info.get("peak_price"),
        "strategy_version": STRATEGY_VERSION,
        "deploy_env": DEPLOY_ENV,
        "buy_tx": entry_info.get("buy_tx"),
        "sell_tx": (sell_result or {}).get("tx"),
        "sell_confirmed": (sell_result or {}).get("confirmed"),
        "entry_usd_actual": entry_usd_actual,
        "exit_usd_actual": exit_usd_actual,
        "pnl_pct_actual": pnl_pct_actual,
    }
    write_journal(record)
    if not is_dry:
        try:
            LIVE_POSITION_MARKER.unlink()
        except FileNotFoundError:
            pass
    pnl_str = f"{pnl_pct:+.2f}%" if pnl_pct is not None else "未知(拿不到退出价)"
    actual_str = f"  真实pnl={pnl_pct_actual:+.2f}%" if pnl_pct_actual is not None else ""
    log(f"台账记录完毕: {exit_reason} pnl={pnl_str}{actual_str} 持仓{hold_sec:.0f}秒 "
       f"(这个币之前进过{entry_info['prior_entries']}次仓)")
    git_push_journal()


def main():
    global LOG_F
    if len(sys.argv) < 3:
        print("用法: python pregrad_scalp_exit.py <池子地址> <mint> <文件前缀>")
        return
    addr = extract_addr(sys.argv[1])
    mint = sys.argv[2]
    prefix = sys.argv[3] if len(sys.argv) > 3 else make_prefix(addr)
    LOG_F = HERE / f"{prefix}_pregrad_scalp.log"

    attrs, mint_check = check_pool_and_mint(addr)
    if not attrs:
        log("查不到这个池子,地址可能不对")
        return
    if mint_check:
        mint = mint_check

    live = is_live_mode()
    mode_str = "实盘" if live else "纸盘"
    log(f"=== 毕业前抢筹{mode_str}: {attrs.get('name')} ({addr[:10]}...) ===")
    log(f"仓位=${POS_SIZE_USD:.2f}({mode_str})  移动止盈回撤={TRAIL_STOP_PCT}%  硬止损={HARD_STOP_LOSS_PCT}%  硬超时={MAX_HOLD_SEC}秒")

    prior_entries = count_prior_entries(mint)
    if prior_entries:
        log(f"注意: 这个币之前已经进过{prior_entries}次仓了,这次是反复进入")

    try:
        entry_price = float(attrs.get("base_token_price_usd") or 0)
    except (TypeError, ValueError):
        entry_price = None
    if not entry_price:
        log(f"拿不到入场价,放弃这次{mode_str}")
        return

    try:
        created = dt.datetime.fromisoformat(attrs["pool_created_at"].replace("Z", "+00:00"))
        coin_age_min = (dt.datetime.now(dt.timezone.utc) - created).total_seconds() / 60
    except (KeyError, ValueError, TypeError):
        coin_age_min = None

    # 2026-07-31新增: 实盘真实买入(PumpPortal构造+本地签名)。买入失败就直接放弃
    # 这个候选,不降级成纸盘——VPS实盘期的数据必须干净。
    # 2026-07-31修复(snipe腿头两笔同一秒建仓的教训): 标记文件必须在买入之前用
    # O_CREAT|O_EXCL原子抢占,不能买完再写——否则多条腿同时发现候选时,1个名额
    # 的限制会被并发竞速绕过,实际下场的钱翻倍。抢不到就放弃,买入失败就释放。
    wallet = None
    buy_tx = None
    post_buy_lamports = None
    sol_price_at_entry = None
    if live:
        try:
            marker_fd = os.open(str(LIVE_POSITION_MARKER), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            log("实盘仓位名额已被其他进程抢占,放弃这个候选")
            return

        def release_marker():
            try:
                os.close(marker_fd)
            except OSError:
                pass
            try:
                LIVE_POSITION_MARKER.unlink()
            except FileNotFoundError:
                pass

        wallet = get_wallet()
        sol_price_at_entry = get_sol_price_usd()
        if not sol_price_at_entry:
            log("拿不到SOL实时价格,不猜,放弃这个候选")
            release_marker()
            return
        buy_tx, buy_ok, buy_err = do_live_buy(wallet, mint, POS_SIZE_USD, sol_price_at_entry)
        if not buy_tx or not buy_ok:
            log(f"实盘买入失败({venue_for(mint)}通道): {buy_err or '交易未确认'} tx={buy_tx},放弃这个候选")
            release_marker()
            return
        log(f"实盘买入成功(≈${POS_SIZE_USD:.2f},{venue_for(mint)}通道) tx={buy_tx}")
        post_buy_lamports = get_wallet_lamports(str(wallet.pubkey()))
        try:
            os.write(marker_fd, json.dumps({
                "addr": addr, "mint": mint, "name": attrs.get("name"),
                "opened_at": time.time(), "entry_price": entry_price,
                "pos_size_usd": POS_SIZE_USD,
            }, ensure_ascii=False).encode("utf-8"))
            os.close(marker_fd)
        except OSError:
            pass

    entry_info = {
        "name": attrs.get("name"), "mint": mint, "addr": addr, "entry_ts": time.time(),
        "coin_age_min_at_entry": coin_age_min, "entry_price": entry_price,
        "entry_liq_usd": attrs.get("reserve_in_usd"), "entry_locked_liq_pct": attrs.get("locked_liquidity_percentage"),
        "entry_fdv_usd": attrs.get("fdv_usd"), "prior_entries": prior_entries, "peak_price": entry_price,
        "dry_run": not live, "buy_tx": buy_tx,
        "entry_usd_actual": POS_SIZE_USD if live else None,
    }

    def live_exit(reason):
        """实盘退出统一入口: 全仓卖出 -> 用钱包余额差算真实到账USD。余额差
        包含了滑点/服务费/优先费/gas的全部真实成本,不做任何理想化假设。"""
        if not live:
            return None
        result = do_live_sell(wallet, mint, reason)
        if result.get("confirmed") and post_buy_lamports is not None:
            post_sell = get_wallet_lamports(str(wallet.pubkey()))
            sol_price_now = get_sol_price_usd() or sol_price_at_entry
            if post_sell is not None and sol_price_now:
                result["exit_usd_actual"] = (post_sell - post_buy_lamports) / 1e9 * sol_price_now
        return result

    log(f"[{mode_str}] 建仓 entry_price={entry_price}  (池子年龄约{coin_age_min:.1f}分钟)" if coin_age_min is not None
        else f"[{mode_str}] 建仓 entry_price={entry_price}")

    peak_price = entry_price
    n_fail_in_a_row = 0
    was_declining = False   # 上一次观察到价格低于峰值,下一次用更短间隔加密复查
    entry_ts = entry_info["entry_ts"]
    deadline = entry_ts + MAX_HOLD_SEC
    while time.time() < deadline:
        time.sleep(FAST_RECHECK_SEC if was_declining else POLL_SEC)
        snap = get_pool_snapshot(addr)
        if not snap or snap.get("price") is None:
            n_fail_in_a_row += 1
            # 2026-07-29: 查不到池子/价格,最可能的原因就是"已经毕业迁移到新池子了"
            # (老的bonding curve地址在GT上要么消失要么不再更新),其次才是纯API抖动。
            # 连续3次查不到,判定为"没跑赢毕业,被留在老池子里"——用最后
            # 已知价格结算,老实记录这次没跑赢,不装作若无其事。
            if n_fail_in_a_row >= 3:
                log("*** 连续查不到池子数据,大概率已经毕业迁移,没能抢在毕业前跑掉 ***")
                entry_info["peak_price"] = peak_price
                # 实盘: 币已随毕业迁移到新场地,do_live_sell的pool=auto会自动路由,
                # 尽快脱手,不像纸盘那样只是记一笔就完事。
                sell_result = live_exit("MISSED_EXIT_LIKELY_GRADUATED")
                finish_trade(entry_info, "MISSED_EXIT_LIKELY_GRADUATED", peak_price, sell_result)
                if not live:
                    handoff_to_post_grad(addr, mint, attrs.get("name"))
                return
            continue
        n_fail_in_a_row = 0
        price = snap["price"]
        was_declining = price < peak_price
        if price > peak_price:
            peak_price = price

        if snap.get("liq") is not None and snap["liq"] < 500:
            log(f"*** 流动性只剩${snap['liq']:,.0f},判定已死透,卖出离场 ***")
            entry_info["peak_price"] = peak_price
            sell_result = live_exit("LIQ_DEAD")
            finish_trade(entry_info, "LIQ_DEAD", price, sell_result)
            return

        drawdown_from_peak = (1 - price / peak_price) * 100 if peak_price else 0
        if peak_price > entry_price and drawdown_from_peak >= TRAIL_STOP_PCT:
            log(f"*** 移动止盈触发: 最高价{peak_price:.10g}回撤{drawdown_from_peak:.1f}%,卖出离场 ***")
            entry_info["peak_price"] = peak_price
            sell_result = live_exit("TRAILING_STOP")
            finish_trade(entry_info, "TRAILING_STOP", price, sell_result)
            return

        loss_from_entry = (1 - price / entry_price) * 100
        if loss_from_entry >= HARD_STOP_LOSS_PCT:
            log(f"*** 硬止损触发: 跌破入场价{loss_from_entry:.1f}%,没被拉起来,止损离场 ***")
            entry_info["peak_price"] = peak_price
            sell_result = live_exit("HARD_STOP_LOSS")
            finish_trade(entry_info, "HARD_STOP_LOSS", price, sell_result)
            return

    log(f"=== 硬超时{MAX_HOLD_SEC}秒到,不管当前盈亏直接离场(不恋战,不赌毕业后的行情) ===")
    final_snap = get_pool_snapshot(addr)
    final_price = final_snap.get("price") if final_snap else peak_price
    entry_info["peak_price"] = peak_price
    sell_result = live_exit("HARD_TIMEOUT")
    finish_trade(entry_info, "HARD_TIMEOUT", final_price, sell_result)


if __name__ == "__main__":
    main()
