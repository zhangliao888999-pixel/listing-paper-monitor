# -*- coding: utf-8 -*-
"""从Phantom的恢复短语，在你自己电脑上离线算出对应的Solana私钥。

**安全须知**：
  - 这个脚本只能在你自己的电脑上运行，输入的恢复短语只会留在这一次运行的内存里，
    脚本不会把它写进任何文件、不会发送到任何网络请求、运行结束就没了。
  - 千万不要把恢复短语打进聊天框、发给任何人（包括Claude）、贴到任何网页。
  - 建议在没有联网的环境下运行更安全，或者至少确认这台电脑没有中木马/键盘记录器。

**工作原理**：Phantom等Solana钱包按BCP39标准把恢复短语转成一个种子，再按一个固定的
"派生路径"算出私钥。不同钱包可能用的具体算法细节略有不同，所以这个脚本会尝试几种
最常见的算法，然后**拿算出来的公钥地址去跟你已经在Phantom里看到的那个真实地址核对**
——对得上的那个才是真正对应你账户的私钥，对不上的直接丢弃不用。

用法: python derive_key_from_seed.py
"""
import getpass
import hashlib

from solders.keypair import Keypair


def mnemonic_to_seed(mnemonic: str, passphrase: str = "") -> bytes:
    """BIP39标准: PBKDF2-HMAC-SHA512, 2048次迭代, 64字节输出"""
    mnemonic_norm = " ".join(mnemonic.split())
    return hashlib.pbkdf2_hmac(
        "sha512", mnemonic_norm.encode("utf-8"),
        ("mnemonic" + passphrase).encode("utf-8"), 2048, dklen=64,
    )


def try_all_methods(mnemonic: str):
    seed64 = mnemonic_to_seed(mnemonic)
    candidates = []
    try:
        kp = Keypair.from_seed_phrase_and_passphrase(mnemonic, "")
        candidates.append(("方法1: 无派生路径(老式Sollet风格)", kp))
    except Exception as e:
        print(f"  方法1 失败: {e}")
    for path, label in [
        ("m/44'/501'/0'/0'", "方法2: 标准BIP44路径 m/44'/501'/0'/0' (Phantom/Solflare常见)"),
        ("m/44'/501'/0'", "方法3: 三段路径 m/44'/501'/0'"),
        ("m/44'/501'/1'/0'", "方法4: 账户索引1 m/44'/501'/1'/0' (如果你的账户不是第一个)"),
    ]:
        try:
            kp = Keypair.from_seed_and_derivation_path(seed64, path)
            candidates.append((label, kp))
        except Exception as e:
            print(f"  {label} 失败: {e}")
    return candidates


def main():
    print("=" * 60)
    print("警告: 接下来会让你输入恢复短语，输入内容不会显示在屏幕上。")
    print("这个操作只在本机内存里进行，不联网、不落盘。")
    print("=" * 60)
    mnemonic = getpass.getpass("请输入Phantom的恢复短语(12或24个单词，空格分隔): ").strip()
    known_addr = input("请输入你在Phantom里已经看到的真实Solana地址(用来核对): ").strip()

    if not mnemonic or not known_addr:
        print("输入为空，退出。")
        return

    print("\n正在尝试几种常见的派生方式...\n")
    candidates = try_all_methods(mnemonic)

    matched = None
    for label, kp in candidates:
        pubkey = str(kp.pubkey())
        hit = " <-- 匹配!" if pubkey == known_addr else ""
        print(f"{label}\n  算出的地址: {pubkey}{hit}\n")
        if pubkey == known_addr:
            matched = (label, kp)

    print("=" * 60)
    if matched:
        label, kp = matched
        print(f"找到匹配: {label}")
        print(f"对应的私钥(base58，这就是要填进 set_env.ps1 里 WALLET_PRIVATE_KEY 的值):")
        print(f"\n  {kp}\n")
        print("请立刻把这个值复制到set_env.ps1里，然后关闭这个终端窗口/清屏，")
        print("不要把这段输出截图、复制粘贴到别的地方保存。")
    else:
        print("没有一种方法算出的地址跟你提供的真实地址匹配。")
        print("可能是恢复短语输入有误(检查单词拼写/顺序)，或者Phantom用了本脚本")
        print("没覆盖到的派生方式——这种情况下不要瞎猜着用，找Phantom官方客服确认，")
        print("或者优先想办法从Phantom里直接导出私钥。")
    print("=" * 60)


if __name__ == "__main__":
    main()
