package com.insta.demo;

import android.content.Intent;
import android.os.Bundle;
import android.widget.Button;
import android.widget.EditText;
import android.widget.TextView;

import androidx.appcompat.app.AppCompatActivity;

public class MainActivity extends AppCompatActivity {

    private int failedAttempts = 0;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        EditText username = findViewById(R.id.username);
        EditText password = findViewById(R.id.password);
        TextView errorText = findViewById(R.id.error_text);
        Button loginBtn = findViewById(R.id.login_btn);

        loginBtn.setOnClickListener(v -> {
            // 故意 bug（对抗集用，勿修）：连续失败 3 次后按钮无禁用态
            String user = username.getText().toString().trim();
            if ("pass1234".contentEquals(password.getText())) {
                errorText.setVisibility(TextView.GONE);
                Intent intent = new Intent(this, ListActivity.class);
                intent.putExtra("user", user.isEmpty() ? "tester" : user);
                startActivity(intent);
            } else {
                failedAttempts++;
                errorText.setVisibility(TextView.VISIBLE);
                errorText.setText(getString(R.string.login_error) + "（" + failedAttempts + "）");
            }
        });

        // 公告入口：**白屏类缺陷的探针入口**。这一页在带缺陷版上**渲染空白**。
        // 放在登录页是刻意的——`explore._candidate()` 跳过 EditText（D26），
        // 它登不进去，放列表页这一页就永远探不到。
        findViewById(R.id.announce_btn).setOnClickListener(
                v -> startActivity(new Intent(this, BlankActivity.class)));
    }
}
