"""本地测试收藏夹 API —— 直接用浏览器抓到的 URL 中的参数验证接口是否通。

用法:
    python test_collect_api.py
"""

import asyncio
import json
import sys

import yaml

from core.api_client import DouyinAPIClient

# 从你给的 URL 中提取的关键参数
COLLECTS_ID = "7650779788835313457"
CURSOR = 0
COUNT = 10


def load_cookies(config_path: str = "config.yml") -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return config.get("cookies", {})


async def main():
    cookies = load_cookies()
    if not cookies:
        print("[ERROR] config.yml 中没有 cookies，请先配置")
        sys.exit(1)

    print(f"msToken: {cookies.get('msToken', 'N/A')[:20]}...")
    print(f"collects_id: {COLLECTS_ID}")
    print(f"cursor={CURSOR}, count={COUNT}")
    print("-" * 60)

    client = DouyinAPIClient(cookies)
    try:
        data = await client.get_collect_aweme(
            collects_id=COLLECTS_ID,
            max_cursor=CURSOR,
            count=COUNT,
        )

        aweme_list = data.get("aweme_list", [])
        has_more = data.get("has_more", False)
        cursor_val = data.get("max_cursor", 0)

        print(f"\n[OK] 请求成功!")
        print(f"   本次返回: {len(aweme_list)} 条视频")
        print(f"   has_more: {has_more}")
        print(f"   max_cursor: {cursor_val}")

        if aweme_list:
            print(f"\n--- 前 3 条视频预览 ---")
            for i, item in enumerate(aweme_list[:3]):
                aweme_id = item.get("aweme_id", "N/A")
                desc = item.get("desc", "")[:40]
                print(f"  [{i+1}] aweme_id={aweme_id}  desc={desc}")

            # 打印第一条的完整 JSON 供调试
            print(f"\n--- 第一条视频完整字段 ---")
            print(json.dumps(aweme_list[0], ensure_ascii=False, indent=2))
        else:
            print("\n[WARN] aweme_list 为空，检查是否需要登录或 cookies 是否过期")

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
