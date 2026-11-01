"""AI 量化交易守门员系统。

两个域，单向数据流：
  * 标签域（``aiquant.label_service``）—— 只读行情 + LLM，只写标签，永不交易。
  * 执行域（``aiquant.gate`` / ``aiquant.execution``）—— 只读标签，确定性裁决，永不调用 LLM。
  * 验证域（``aiquant.validation``）—— 离线回放与 WFA，禁止被实盘路径导入。
"""

__version__ = "1.0.0"
