import os
import tempfile

# 在导入任何 agenteval 模块前固定数据目录，避免污染项目根目录
_TEST_ROOT = os.path.join(tempfile.gettempdir(), "agenteval-test-data")
os.environ["AGENTEVAL_DATA_DIR"] = _TEST_ROOT
os.environ.setdefault("AGENTEVAL_LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
# 确保测试环境无 key（MissingCredentialsError 相关用例自己再显式清理）
os.environ.pop("AGENTEVAL_LLM_API_KEY", None)
