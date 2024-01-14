from datetime import datetime
from logging import Logger, getLogger
from logging.config import dictConfig
from typing import Tuple
from settings.router import API_ROUTERS
from socket import socket, AF_INET, SOCK_STREAM
from settings.logger import LOGGER_CONFIG
from settings.settings import Settings
from controller.arguments import Arguments
from controller.publisher import Publisher
from controller.server import Server
from controller.decoder import ADSBDecoder, msg_crc, socket_find
from _thread import start_new_thread
from sys import exit
from model.packet import ADSBPacket


def graceful_shutdown(sock: socket, logger: Logger) -> None:
    """优雅关闭 TCP 连接

    收到系统信号 SIGINT 时，关闭 TCP 连接

    Args:
        sock (socket): 创建好的 socket 实例
        logger (Logger): 创建好的日志记录器

    Returns:
        None
    """
    sock.close()
    logger.info("TCP connection has been closed")


def decoder_daemon(sock: socket, packet: ADSBPacket, decoder: ADSBDecoder) -> None:
    """从 Socket 中读取并解析报文

    Args:
        sock (socket): 创建好的已打开的 socket 实例
        packet (ADSBPacket): ADS-B 报文缓冲区

    Returns:
        None
    """
    while True:
        # 取得报文头部 *，长度为 1 字节
        _, err = socket_find(sock, b"*")
        if err:
            continue
        # 接收剩余报文内容并检查完整性
        data_recv = sock.recv(29)
        if data_recv[-1:] != b";":
            continue
        msg = data_recv[:-1].decode("utf-8")
        # 检查 Downlink Format，判断是否可解码
        df = int(msg[:2], 16) >> 3
        decodable = df == 17 or df == 20 or df == 21
        if not decodable:
            continue
        # Downlink Format 为 17 时需 CRC 校验
        _, err = msg_crc(msg)
        if err and df == 17:
            continue
        # 过滤非 Downlink Format 为 17 的报文
        if df != 17:
            continue
        decoder.set_msg(msg)
        # 赋值解码后的数据
        packet.message = msg
        packet.icao, _ = decoder.get_icao()
        packet.callsign, _ = decoder.get_callsign()
        packet.altitude, _ = decoder.get_altitude()
        packet.timestamp = int(datetime.now().timestamp() * 1000)


def connect_tcpserver(host: str, port: int, timeout: int) -> Tuple[socket, bool]:
    """连接 TCP 服务器

    Args:
        host (str): 服务器地址
        port (int): 服务器端口
        timeout (int): 连接超时时间

    Returns:
        Tuple[socket, bool]: 已连接的 socket 实例，连接是否失败
    """
    sock = socket(AF_INET, SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return sock, False
    except:
        return sock, True


def main():
    # 取得全局日志记录器
    dictConfig(LOGGER_CONFIG)
    logger = getLogger("global_logger")

    # 解析命令行参数
    args = Arguments()
    args.parse()

    # 解析配置文件
    conf = Settings()
    err = conf.parse(args.path)
    if err:
        logger.info("Failed to parse config")
        exit(1)

    # 连接报文服务器
    logger.info("Connecting to ADS-B server...")
    source_host, source_port = conf.source.host, conf.source.port
    sock, err = connect_tcpserver(
        source_host, source_port,
        conf.source.timeout,
    )
    if err:
        logger.info(f"Failed to connect to {source_host}:{source_port}")
        exit(1)
    logger.info(f"Connected to {source_host}:{source_port}")

    # 启动报文解析线程，创建发布者
    packet = ADSBPacket()
    decoder = ADSBDecoder()
    publisher = Publisher(packet)
    start_new_thread(decoder_daemon, (sock, packet, decoder))

    # 创建 HTTP 服务器
    server_host, server_port = conf.server.host, conf.server.port
    server_cors, server_debug = conf.server.cors, conf.server.debug
    server = Server(
        host=server_host, port=server_port,
        cors=server_cors, debug=server_debug,
    )
    # 注册系统信号处理函数
    server.on("shutdown", lambda: graceful_shutdown(sock, logger))

    # 注册 API 路由
    for router in API_ROUTERS:
        server.route(router, publisher)
    # 启动地图瓦片服务
    server.static(path="/", dir="./view")

    # 启动 HTTP 服务器线程
    server.start()


if __name__ == '__main__':
    main()
