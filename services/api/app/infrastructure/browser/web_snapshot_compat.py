"""Web 采样的局部 CSS 标识符转义；不修改页面全局对象。"""

_LOCAL_CSS_ESCAPE = r"""
          const CSS = {escape: value => {
            const text = String(value);
            let result = '';
            for (let index = 0; index < text.length; index++) {
              const code = text.charCodeAt(index);
              const char = text.charAt(index);
              if (code === 0) { result += '\ufffd'; continue; }
              if ((code >= 1 && code <= 31) || code === 127
                  || (index === 0 && code >= 48 && code <= 57)
                  || (index === 1 && code >= 48 && code <= 57 && text.charAt(0) === '-')) {
                result += '\\' + code.toString(16) + ' '; continue;
              }
              if (index === 0 && char === '-' && text.length === 1) {
                result += '\\-'; continue;
              }
              if (code >= 128 || char === '-' || char === '_'
                  || (code >= 48 && code <= 57) || (code >= 65 && code <= 90)
                  || (code >= 97 && code <= 122)) result += char;
              else result += '\\' + char;
            }
            return result;
          }};
"""


def prepare_web_snapshot_script(script: str) -> str:
    """仅由 Web 分支调用，闭包内部遮蔽不可信的全局 CSS。"""
    marker = "async () => {"
    if script.count(marker) != 1:
        raise ValueError("Web snapshot 求值入口不唯一")
    return script.replace(marker, marker + _LOCAL_CSS_ESCAPE, 1)
