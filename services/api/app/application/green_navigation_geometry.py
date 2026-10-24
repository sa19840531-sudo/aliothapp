"""用既有导航矩形确认等分区域，不推断文字、可滚动行为或初始状态。"""

from decimal import Decimal


def centered_in_equal_regions(container, boxes):
    """等宽控件可保留留白，但中心须精确落在容器各等分区域的中心。"""
    if container is None or not 2 <= len(boxes) <= 4:
        return False
    ordered = sorted(boxes)
    left, _, width, _ = (Decimal(str(value)) for value in container)
    count = len(ordered)
    for index, box in enumerate(ordered):
        x, _, slot_width, _ = (Decimal(str(value)) for value in box)
        if slot_width * count > width:
            return False
        # 交叉相乘避免除不尽，也不把采集精度改成新的产品合格容差。
        if count * (2 * (x - left) + slot_width) != (2 * index + 1) * width:
            return False
    return True


def fully_in_viewport(container, viewport):
    """父容器部分离屏时，不能把整个父矩形当作当前可用导航区域。"""
    if container is None:
        return False
    x, y, width, height = (Decimal(str(value)) for value in container)
    return (
        x >= 0 and y >= 0
        and x + width <= Decimal(str(viewport["width"]))
        and y + height <= Decimal(str(viewport["height"]))
    )
