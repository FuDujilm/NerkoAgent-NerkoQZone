class _ComponentRegistry:
    def get_plugin_config(self, name: str):
        # 返回 None 表示未找到特定 plugin config（调用方应做容错）
        return None


component_registry = _ComponentRegistry()
