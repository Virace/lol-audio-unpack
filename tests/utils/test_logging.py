"""日志初始化行为测试。"""

from __future__ import annotations

from io import StringIO
from pathlib import Path

from loguru import logger

import lol_audio_unpack.utils.logging as logging_module


def test_setup_logging_tolerates_missing_stderr_in_windowed_mode(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """当 ``sys.stderr`` 缺失时，日志初始化不应再抛出 ``TypeError``。"""
    add_calls: list[tuple[object, dict[str, object]]] = []

    def fake_add(*args, **kwargs):
        sink = args[0]
        add_calls.append((sink, kwargs))
        if sink is None:
            raise TypeError("Cannot log to objects of type 'NoneType'")
        return 1

    monkeypatch.setattr(logging_module.logger, "add", fake_add)
    monkeypatch.setattr(logging_module.logger, "remove", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(logging_module.sys, "stderr", None)

    logging_module.setup_logging(
        dev_mode=False,
        log_level="INFO",
        file_log_level="DEBUG",
        log_file_path=tmp_path / "logs",
    )

    assert add_calls
    assert callable(add_calls[0][0])
    assert add_calls[1][0] == (tmp_path / "logs" / "{time:YYYY-MM-DD_HH-mm-ss}.log")


def test_setup_logging_accepts_explicit_log_file_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """当传入具体日志文件时，应直接复用该文件而不是生成新的时间文件。"""
    add_calls: list[tuple[object, dict[str, object]]] = []

    def fake_add(*args, **kwargs):
        add_calls.append((args[0], kwargs))
        return 1

    monkeypatch.setattr(logging_module.logger, "add", fake_add)
    monkeypatch.setattr(logging_module.logger, "remove", lambda *_args, **_kwargs: None)

    explicit_log_file = tmp_path / "logs" / "session.log"
    logging_module.setup_logging(
        dev_mode=False,
        log_level="INFO",
        file_log_level="DEBUG",
        log_file_path=explicit_log_file,
    )

    assert add_calls[1][0] == explicit_log_file


def test_roots_are_confirmed_once_and_messages_use_relative_paths(tmp_path, monkeypatch) -> None:
    """根目录确认后，控制台和文件正文使用可定位的相对路径。"""
    game = tmp_path / "game"
    output = tmp_path / "output"
    monkeypatch.setattr(logging_module.LoggingConfiguration, "paths", ())
    monkeypatch.setattr(logging_module.LoggingConfiguration, "path_patterns", ())
    console = StringIO()
    monkeypatch.setattr(logging_module.sys, "stderr", console)
    log_file = tmp_path / "session.log"
    logger.enable("lol_audio_unpack")
    logging_module.setup_logging(log_file_path=log_file, show_function_info=False)
    try:
        logging_module.log_paths(game, output)
        logging_module.log_paths(game, output)
        logger.info("读取：{}", game / "Game" / "content-metadata.json")
        logger.debug("写入：{}", output / "hashes" / "4.msgpack")
        logger.complete()
        text = log_file.read_text(encoding="utf-8")
    finally:
        logger.remove()

    assert text.count(str(game)) == 1
    assert text.count(str(output)) == 1
    assert str(Path("Game") / "content-metadata.json") in text
    assert str(Path("hashes") / "4.msgpack") in text
    assert "hashes" not in console.getvalue()
    assert str(game / "Game") not in console.getvalue()
