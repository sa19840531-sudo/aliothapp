"""校验最终 Compose 配置中的部署镜像，不读取或输出业务环境配置。"""

from __future__ import annotations

import argparse
import json
import sys

SERVICE_IMAGES = {
    "alioth-migrate": "api",
    "alioth-initialize": "api",
    "alioth-api": "api",
    "alioth-worker": "api",
    "alioth-workstation": "workstation",
    "alioth-gateway": "gateway",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="校验正式部署的六个服务镜像")
    parser.add_argument("--image-prefix", required=True)
    parser.add_argument("--image-tag", required=True)
    args = parser.parse_args()
    try:
        config = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        print("无法解析最终 Compose 镜像配置。", file=sys.stderr)
        return 1
    services = config.get("services") if isinstance(config, dict) else None
    if not isinstance(services, dict):
        print("最终 Compose 配置缺少 services 对象。", file=sys.stderr)
        return 1

    matched = True
    for service_name, image_name in SERVICE_IMAGES.items():
        service = services.get(service_name)
        actual_image = service.get("image") if isinstance(service, dict) else None
        expected_image = f"{args.image_prefix}/{image_name}:{args.image_tag}"
        if actual_image != expected_image:
            # 只输出镜像身份，不输出可能含密钥的完整 Compose 配置。
            print(
                f"部署编排镜像版本不一致: {service_name} "
                f"expected={expected_image} actual={actual_image}",
                file=sys.stderr,
            )
            matched = False
    return 0 if matched else 1


if __name__ == "__main__":
    raise SystemExit(main())
