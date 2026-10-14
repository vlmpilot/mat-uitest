package com.insta.demo;

import android.os.Bundle;
import android.widget.FrameLayout;

import androidx.appcompat.app.AppCompatActivity;

/**
 * 公告页。
 *
 * ⚠️ 故意 bug（对抗集用，勿修）：**渲染空白**（内容区什么都没画）—— 白屏。
 * 正确行为：显示标题 + 公告正文（见 v09-defect-free 的修复）。
 *
 * 这是对抗集里**唯一一个"白屏类"缺陷**。它的判据**不是 DSL 断言**
 * （DSL 分不清"空白页"和"走错页"），而是 **explore 的 `_screen_suspicious`**：
 * 截图的**灰度极差 < 6** 判为可疑，再按均值分白/黑屏。
 *
 * ⚠️ 入口放在**登录页**（`MainActivity`）而不是列表页：
 * `explore._candidate()` 跳过 `EditText`（台账 D26）⇒ 它**永远登不进去**，
 * 放列表页就永远探不到这一页。
 */
public class BlankActivity extends AppCompatActivity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // 故意 bug（对抗集用，勿修）：一个纯白、无任何子控件的容器。
        FrameLayout blank = new FrameLayout(this);
        blank.setBackgroundColor(0xFFFFFFFF);
        setContentView(blank);
    }
}
