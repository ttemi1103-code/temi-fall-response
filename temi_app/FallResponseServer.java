package com.example.fallresponse; // 改成組員 App 的套件名稱

import android.os.Handler;
import android.os.Looper;
import android.util.Log;

import com.robotemi.sdk.Robot;
import com.robotemi.sdk.listeners.OnGoToLocationStatusChangedListener;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * 讓電腦上的 Python 程式透過 Wi-Fi 派遣 Temi，並查詢是否已抵達。
 *
 * <pre>
 * GET  /locations  → {"locations": ["vr", "zone2", ...]}
 * POST /goto       {"location": "vr"} → {"request_id": "3", "location": "vr"}
 * GET  /status     → {"request_id": "3", "location": "vr", "status": "going", "description": "", "updated_ms": ...}
 * POST /stop       → {"ok": true}
 * </pre>
 *
 * status 直接使用 Temi SDK 的導航狀態：start、calculating、going、complete（已抵達）、abort（中止）、reposing，
 * 尚未收到 SDK 回報前為 requested，從未派遣時為 idle。
 *
 * 使用方式：在 onRobotReady(true) 之後呼叫 start()，在 onDestroy() 呼叫 stop()。
 * AndroidManifest.xml 需要 &lt;uses-permission android:name="android.permission.INTERNET" /&gt;。
 */
public class FallResponseServer implements OnGoToLocationStatusChangedListener {
    private static final String TAG = "FallResponseServer";
    public static final int DEFAULT_PORT = 8080;
    private static final int MAX_BODY_BYTES = 16 * 1024;
    private static final int SOCKET_TIMEOUT_MS = 5000;

    private final Robot robot;
    private final int port;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private ServerSocket serverSocket;
    private volatile boolean running;

    // 目前派遣的導航狀態，由 SDK 監聽器更新
    private int requestCounter = 0;
    private String requestId = "";
    private String targetLocation = "";
    private String status = "idle";
    private String description = "";
    private long updatedMs = System.currentTimeMillis();

    public FallResponseServer(Robot robot) {
        this(robot, DEFAULT_PORT);
    }

    public FallResponseServer(Robot robot, int port) {
        this.robot = robot;
        this.port = port;
    }

    public synchronized void start() throws IOException {
        if (running) {
            return;
        }
        robot.addOnGoToLocationStatusChangedListener(this);
        serverSocket = new ServerSocket(port);
        running = true;
        Thread acceptThread = new Thread(new Runnable() {
            @Override
            public void run() {
                acceptLoop();
            }
        }, "fall-response-server");
        acceptThread.setDaemon(true);
        acceptThread.start();
        Log.i(TAG, "已啟動，port " + port);
    }

    public synchronized void stop() {
        running = false;
        robot.removeOnGoToLocationStatusChangedListener(this);
        if (serverSocket != null) {
            try {
                serverSocket.close();
            } catch (IOException ignored) {
            }
        }
    }

    @Override
    public void onGoToLocationStatusChanged(String location, String newStatus, int descriptionId, String newDescription) {
        synchronized (this) {
            // 只記錄這次派遣的地點；Temi 會把地點名稱轉成小寫，所以比對時忽略大小寫
            if (!location.equalsIgnoreCase(targetLocation)) {
                return;
            }
            status = newStatus;
            description = newDescription == null ? "" : newDescription;
            updatedMs = System.currentTimeMillis();
        }
        Log.i(TAG, "導航狀態：" + location + " → " + newStatus + " " + newDescription);
    }

    private void acceptLoop() {
        while (running) {
            try {
                final Socket socket = serverSocket.accept();
                new Thread(new Runnable() {
                    @Override
                    public void run() {
                        handle(socket);
                    }
                }).start();
            } catch (IOException e) {
                if (running) {
                    Log.w(TAG, "接收連線失敗", e);
                }
            }
        }
    }

    private void handle(Socket socket) {
        try {
            socket.setSoTimeout(SOCKET_TIMEOUT_MS);
            InputStream in = new BufferedInputStream(socket.getInputStream());
            String requestLine = readLine(in);
            if (requestLine == null) {
                return;
            }
            String[] parts = requestLine.split(" ");
            if (parts.length < 2) {
                respond(socket, 400, error("格式錯誤"));
                return;
            }
            String method = parts[0];
            String path = parts[1];
            int contentLength = 0;
            String header;
            while ((header = readLine(in)) != null && !header.isEmpty()) {
                int colon = header.indexOf(':');
                if (colon > 0 && header.substring(0, colon).trim().equalsIgnoreCase("Content-Length")) {
                    contentLength = Integer.parseInt(header.substring(colon + 1).trim());
                }
            }
            if (contentLength > MAX_BODY_BYTES) {
                respond(socket, 413, error("內容過大"));
                return;
            }
            String body = new String(readBytes(in, contentLength), StandardCharsets.UTF_8);
            route(socket, method, path, body);
        } catch (Exception e) {
            Log.w(TAG, "處理請求失敗", e);
            try {
                respond(socket, 500, error(String.valueOf(e.getMessage())));
            } catch (IOException ignored) {
            }
        } finally {
            try {
                socket.close();
            } catch (IOException ignored) {
            }
        }
    }

    private void route(Socket socket, String method, String path, String body) throws IOException, JSONException {
        if (method.equals("GET") && path.equals("/locations")) {
            JSONObject result = new JSONObject();
            result.put("locations", new JSONArray(locations()));
            respond(socket, 200, result);
        } else if (method.equals("GET") && path.equals("/status")) {
            respond(socket, 200, statusJson());
        } else if (method.equals("POST") && path.equals("/goto")) {
            goTo(socket, body);
        } else if (method.equals("POST") && path.equals("/stop")) {
            mainHandler.post(new Runnable() {
                @Override
                public void run() {
                    robot.stopMovement();
                }
            });
            respond(socket, 200, new JSONObject().put("ok", true));
        } else {
            respond(socket, 404, error("沒有這個功能：" + method + " " + path));
        }
    }

    private void goTo(Socket socket, String body) throws IOException, JSONException {
        final String location = new JSONObject(body).optString("location", "").trim();
        List<String> available = locations();
        String matched = null;
        for (String name : available) {
            if (name.equalsIgnoreCase(location)) {
                matched = name;
            }
        }
        if (matched == null) {
            JSONObject result = error("Temi 上沒有地點：" + location);
            result.put("locations", new JSONArray(available));
            respond(socket, 404, result);
            return;
        }
        final String target = matched;
        JSONObject result;
        synchronized (this) {
            requestCounter += 1;
            requestId = String.valueOf(requestCounter);
            targetLocation = target;
            status = "requested";
            description = "";
            updatedMs = System.currentTimeMillis();
            result = new JSONObject().put("request_id", requestId).put("location", target);
        }
        // SDK 指令放在主執行緒執行
        mainHandler.post(new Runnable() {
            @Override
            public void run() {
                robot.goTo(target);
            }
        });
        Log.i(TAG, "派遣 Temi 前往 " + target);
        respond(socket, 200, result);
    }

    private List<String> locations() {
        List<String> result = robot.getLocations();
        return result == null ? new ArrayList<String>() : result;
    }

    private synchronized JSONObject statusJson() throws JSONException {
        return new JSONObject()
                .put("request_id", requestId)
                .put("location", targetLocation)
                .put("status", status)
                .put("description", description)
                .put("updated_ms", updatedMs);
    }

    private static JSONObject error(String message) {
        try {
            return new JSONObject().put("error", message);
        } catch (JSONException e) {
            return new JSONObject();
        }
    }

    private static void respond(Socket socket, int code, JSONObject body) throws IOException {
        byte[] bytes = body.toString().getBytes(StandardCharsets.UTF_8);
        String head = String.format(Locale.US,
                "HTTP/1.1 %d %s\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: %d\r\nConnection: close\r\n\r\n",
                code, code == 200 ? "OK" : "Error", bytes.length);
        OutputStream out = socket.getOutputStream();
        out.write(head.getBytes(StandardCharsets.US_ASCII));
        out.write(bytes);
        out.flush();
    }

    private static String readLine(InputStream in) throws IOException {
        ByteArrayOutputStream line = new ByteArrayOutputStream();
        int b;
        while ((b = in.read()) != -1) {
            if (b == '\n') {
                break;
            }
            if (b != '\r') {
                line.write(b);
            }
        }
        if (b == -1 && line.size() == 0) {
            return null;
        }
        return new String(line.toByteArray(), StandardCharsets.UTF_8);
    }

    private static byte[] readBytes(InputStream in, int length) throws IOException {
        byte[] data = new byte[length];
        int read = 0;
        while (read < length) {
            int n = in.read(data, read, length - read);
            if (n == -1) {
                throw new IOException("內容不完整");
            }
            read += n;
        }
        return data;
    }
}
