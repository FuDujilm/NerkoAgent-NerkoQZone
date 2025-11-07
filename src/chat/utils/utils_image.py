class _DummyImageManager:
    async def get_image_description(self, image_base64: str) -> str:
        # 返回空描述，保证代码能继续运行
        return ""


def get_image_manager():
    return _DummyImageManager()
