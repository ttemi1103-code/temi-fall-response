package com.example.fallresponse; // 改成 App 的套件名稱

import android.app.Activity;
import android.os.Bundle;
import android.util.Log;
import android.widget.TextView;

import com.robotemi.sdk.Robot;
import com.robotemi.sdk.listeners.OnRobotReadyListener;

import java.io.IOException;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.NetworkInterface;
import java.util.Collections;

/**
 * 跌倒派遣 App 的畫面：Temi 準備好後啟動 FallResponseServer，並在畫面上顯示電腦要連的網址。
 * 如果已經有自己的 MainActivity，可以只把 onRobotReady() 和 startServer() 的內容合併進去。
 */
public class MainActivity extends Activity implements OnRobotReadyListener {
    private static final String TAG = "MainActivity";

    private Robot robot;
    private FallResponseServer server;
    private TextView statusView;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        robot = Robot.getInstance();
        statusView = new TextView(this);
        statusView.setTextSize(24);
        statusView.setPadding(48, 48, 48, 48);
        statusView.setText("等待 Temi 準備好……");
        setContentView(statusView);
    }

    @Override
    protected void onStart() {
        super.onStart();
        robot.addOnRobotReadyListener(this);
    }

    @Override
    protected void onStop() {
        super.onStop();
        robot.removeOnRobotReadyListener(this);
    }

    @Override
    public void onRobotReady(boolean isReady) {
        if (isReady && server == null) {
            startServer();
        }
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        if (server != null) {
            server.stop();
        }
    }

    private void startServer() {
        server = new FallResponseServer(robot);
        try {
            server.start();
            statusView.setText("跌倒派遣服務已啟動\n\n電腦連線網址：\nhttp://" + localIp() + ":"
                    + FallResponseServer.DEFAULT_PORT + "\n\nTemi 上的地點：" + robot.getLocations());
        } catch (IOException e) {
            Log.e(TAG, "跌倒派遣服務啟動失敗", e);
            statusView.setText("跌倒派遣服務啟動失敗：" + e.getMessage());
            server = null;
        }
    }

    /** 找出 Wi-Fi 的 IPv4 位址，顯示在畫面上方便電腦連線。 */
    private static String localIp() {
        try {
            for (NetworkInterface network : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                for (InetAddress address : Collections.list(network.getInetAddresses())) {
                    if (!address.isLoopbackAddress() && address instanceof Inet4Address) {
                        return address.getHostAddress();
                    }
                }
            }
        } catch (Exception e) {
            Log.w(TAG, "找不到 IP", e);
        }
        return "（請到 Temi 設定 → 開發者工具查看）";
    }
}
