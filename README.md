# rootlessxray

不用 root。每个人把一行订阅地址放进自己的 `~/rootlessxray/subscription`，再启动一份只属于自己的 Xray。程序只监听本机端口，当前终端用 `source` 接上代理。

能打开 ChatGPT 的节点才会留下。返回 HTTP 401 或 403 算通过。

目录里已经带了 Linux 64 位的 `xray`，版本是 [Xray-core 26.3.27](https://github.com/XTLS/Xray-core/releases/tag/v26.3.27)。这是上游原版程序，没有改过。克隆或下载后不用再单独下载。

## 使用

把下面命令里的路径换成这份代码所在的目录。

先打开一个终端

```bash
mkdir -p ~/rootlessxray
nano ~/rootlessxray/subscription #或者直接打开subscription把订阅复制进去
source /path/to/rootlessxray/start.sh #这样就在本终端翻出去了
```

`subscription` 里只写一行自己的订阅地址。

再新开一个终端时，代理进程还在，只要：

```bash
source /path/to/rootlessxray/proxy_env.sh
```

换订阅地址时，改 `~/rootlessxray/subscription`，再 `source` 一次 `start.sh`。

## 终端界面

想看测通的节点、固定其中一个，或者改回自动选择：

```bash
/path/to/rootlessxray/tui.sh
```

方向键移动。Enter 固定当前节点，`a` 恢复自动选择，`s` 按当前配置启动，`u` 重新拉订阅并探测，`q` 退出。退出后这个终端还要再 `source proxy_env.sh`。

## 这台机器上

程序已经放在 `/disk227/lixingqi/software/rootlessxray`，`xray` 也在。同学直接：

```bash
mkdir -p ~/rootlessxray
nano ~/rootlessxray/subscription
source /disk227/lixingqi/software/rootlessxray/start.sh
```

新开终端：

```bash
source /disk227/lixingqi/software/rootlessxray/proxy_env.sh
```

看节点和固定节点：

```bash
/disk227/lixingqi/software/rootlessxray/tui.sh
```

日志在 `/disk227/lixingqi/software/rootlessxray/log/用户名/`。

