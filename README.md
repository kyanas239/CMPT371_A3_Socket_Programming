# **CMPT 371 A3 Socket Programming**

# 📻 WalkiePy — `Walkie-Talkie Voice Chat`
> A real-time push-to-talk (PTT) Walkie-Talkie voice chat application built
> with Python's Socket API, PyAudio, and Tkinter.

**Course:** CMPT 371 \- Data Communications & Networking  
**Instructor:** Mirza Zaeem Baig  
**Semester:** Spring 2026  

## **Group Members**

| Name | Student ID | Email |
| :---- | :---- | :---- |
| Ramneet Bhangoo | 301565796 |  |
| Kyana Sohangar | 301604899 | kyana_sohangar@sfu.ca |

## **1\. Project Overview & Description**

WalkiePy is a real-time push-to-talk (PTT) voice chat application built using Python's Socket API (UDP `SOCK_DGRAM`). It allows multiple clients to connect to a central relay server and join a shared audio channel, simulating the half-duplex behaviour of a physical walkie-talkie. A client holds the **TRANSMIT** button (or the **SPACE** key) to broadcast their microphone audio to all other connected clients in real time.

The server handles client registration, audio relay, chat broadcast, and presence detection via heartbeats — ensuring that disconnected clients are automatically evicted without requiring any action from remaining participants.

### **Additional:** 
The Tkinter GUI includes a live user list, showing who is currently in the channel and a text chat panel for users to communicate in written messages, in addition to the voice chat.

The project demonstrates core networking concepts:
- **Socket programming** (UDP `SOCK_DGRAM`)
- **Client–server architecture** with a broadcast relay
- **Concurrent threading** for audio I/O, network I/O, and GUI
- **Custom binary protocol** over raw sockets
- **Heartbeat-based presence detection**

## **2\. System Limitations & Edge Cases**

As required by the project specifications, we have identified and handled (or defined) the following limitations and potential issues within our application scope:

* **No Audio Compression**
*Limitation:* Raw 16-bit PCM at 16 kHz = ~32 KB/s per transmitting client. On a local LAN this is fine; it may be noticeable over the internet.  
*Solution:* Integrate an opus codec (e.g. `opuslib`) to compress audio ~10× with no perceptible quality loss.

* **No Encryption**
*Limitation:* Audio packets are sent as raw PCM over the network. Anyone on the same network with a packet sniffer (e.g. Wireshark) can capture and replay voice data.  
*Solution:* Wrap the socket in DTLS (Datagram TLS), or encrypt each payload with a shared AES key before sending.

* **Server is a Single Point of Failure**
*Limitation:* If the server crashes, all clients lose connectivity immediately.  
*Solution:* Add a reconnection loop in the client (exponential backoff), and consider a peer-to-peer fallback or a redundant server.

* **Scalability**
*Limitation:* The server relays every audio packet to every other client. With N clients, each transmit causes N−1 sends. At large scale this becomes O(N) bandwidth per packet.  
*Solution:* Use IP multicast (UDP `setsockopt SO_IP_MULTICAST`) to let the network layer handle fan-out, reducing server send load to 1.

* **No Username Authentication**
*Limitation:* Any client can register with any username, including impersonating another user.  
*Solution:* Implement a token/challenge handshake on registration, or a simple password for the channel.

### Abrupt Client Disconnection
*Limitation:* If a client process is killed without sending `PKT_DISCONNECT`, the server won't know until the heartbeat timeout (8 seconds).  
*Mitigation already in place:* The cleanup thread evicts clients after `HEARTBEAT_TIMEOUT` seconds of silence. This value is tunable in `server.py`.

## **3\. Video Demo**

Our 2-minute video demonstration covering connection establishment, data exchange, real-time voice chat, and process termination can be viewed below:  
[**▶️ Watch Project Demo on YouTube**](https://www.youtube.com/watch?v=dQw4w9WgXcQ)

## **4\. Prerequisites (Fresh Environment)**

To run this project, you need:

* **Python 3.10** or higher.  
* VS Code. 
* requirements.txt file is included for environment completeness.  

### **Step 1 — Install PortAudio (system library for PyAudio)**

**macOS**
```bash
brew install portaudio
```

**Windows**  
PyAudio wheels for Windows come bundled with PortAudio; the `pip install`
in the next step is sufficient. If you encounter errors, install a prebuilt
wheel:
```bash
pip install pipwin
pipwin install pyaudio
```

### **Step 2 — Clone / download the project**

```bash
git clone https://github.com/kyanas239/CMPT371_A3_Socket_Programming.git
cd CMPT371_A3_Socket_Programming
```

Or unzip the downloaded archive and `cd` into it.

### **Step 3 — Create a virtual environment (Optional)**

Using a venv keeps dependencies isolated:

```bash
python3 -m venv venv
source venv/bin/activate     # macOS
venv\Scripts\activate.bat    # Windows
```

### **Step 4 — Install Python dependencies**

```bash
pip install -r requirements.txt
```

> **Verify** that PyAudio installed correctly:
> ```bash
> python3 -c "import pyaudio; print('PyAudio OK')"
> ```

## **5\. Running the Application**

### **Start the Server**
On the machine that will act as the relay hub:

```bash
python3 server.py
```

To bind to a specific interface or use a non-default port:

```bash
python3 server.py --host 0.0.0.0 --port 9000
```

You should see:
```
12:00:00  [INFO]  Server listening on 0.0.0.0:9000  (UDP)
12:00:00  [INFO]  Server ready. Press Ctrl+C to stop.
```

### **Connect Clients**
On each client machine (can be the same machine as the server for testing):

```bash
python3 client.py
```

A connection dialog will appear. Fill in:

| Field | Value |
|---|---|
| **Server IP** | IP address of the machine running `server.py` |
| **Port** | `9000` (or what you used with `--port`) |
| **Callsign** | Your display name in the channel |

Click **CONNECT**.

### **Testing Locally (Two Clients on One Machine)**
Open two terminal windows and run `python3 client.py` in each. Use
`127.0.0.1` as the server IP. This lets you test the full PTT and chat flow
without any additional hardware.

### **Using PTT**
- **Hold SPACE** (when the chat input is not focused) — or —
- **Click and hold the TRANSMIT button**

Release to stop transmitting. Other connected clients will hear you.


## **6\. Academic Integrity & References**

* **Code Origin:**  
  * The socket boilerplate was adapted from the course tutorial "TCP Echo Server". The core multithreaded game logic, protocol, and state management were written by the group.  
* **GenAI Usage:**  
  * ChatGPT was used to assist in generating the Unicode box-drawing characters for the CLI interface, and to help structure the TCP buffer-splitting logic (`\n delimiter`).  
  * Gemini was used to help in `README.md` writing and polishing.  
  * GitHub Copilot was used to help plan the workflow of the application.   
* **References:**  
  * [Python Socket Programming HOWTO](https://docs.python.org/3/howto/sockets.html)  
  * [Real Python: Intro to Python Threading](https://realpython.com/intro-to-python-threading/)