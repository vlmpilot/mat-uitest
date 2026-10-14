// MAT iOS 演示靶场（对应 Android examples/demo-app 的角色）。
// 覆盖引擎需要的页面/控件形态：导航、文本输入、按钮禁用态（状态断言）、
// 开关（checked/selected 断言）、列表行 + 长按菜单（U8 阈值标定）、详情页。
// 定位锚点全部埋 accessibilityIdentifier（= WDA 的 name），文案与 id 双轨——
// 自愈/缺陷变体（rename id / rename 文案）按 demo-app 同法做。

import SwiftUI

struct HomeView: View {
    var body: some View {
        List {
            NavigationLink("登录", destination: LoginView())
                .accessibilityIdentifier("nav_login")
            NavigationLink("表单", destination: FormView())
                .accessibilityIdentifier("nav_form")
            NavigationLink("列表", destination: ListView())
                .accessibilityIdentifier("nav_list")
            NavigationLink("开关", destination: ToggleView())
                .accessibilityIdentifier("nav_toggle")
        }
        .navigationTitle("MAT Demo")
    }
}

struct LoginView: View {
    @State private var user = ""
    @State private var pass = ""
    @State private var loggedIn = false

    var body: some View {
        VStack(spacing: 16) {
            TextField("用户名", text: $user)
                .accessibilityIdentifier("login_user")
                .textFieldStyle(.roundedBorder)
            SecureField("密码", text: $pass)
                .accessibilityIdentifier("login_pass")
                .textFieldStyle(.roundedBorder)
            Button("登录") { loggedIn = true }
                .accessibilityIdentifier("login_btn")
                .disabled(user.isEmpty || pass.isEmpty)
            if loggedIn {
                Text("登录成功：\(user)")
                    .accessibilityIdentifier("login_ok")
            }
        }
        .padding()
        .navigationTitle("登录")
    }
}

struct FormView: View {
    @State private var name = ""
    @State private var count = 1
    @State private var submitted = false

    var body: some View {
        VStack(spacing: 16) {
            TextField("姓名", text: $name)
                .accessibilityIdentifier("form_name")
                .textFieldStyle(.roundedBorder)
            Stepper("数量 \(count)", value: $count, in: 1...9)
                .accessibilityIdentifier("form_count")
            Button("提交") { submitted = true }
                .accessibilityIdentifier("form_submit")
            if submitted {
                Text("已提交 \(name) × \(count)")
                    .accessibilityIdentifier("form_ok")
            }
        }
        .padding()
        .navigationTitle("表单")
    }
}

struct ListView: View {
    let items = (1...20).map { "项目 \($0)" }

    var body: some View {
        List {
            ForEach(items, id: \.self) { item in
                NavigationLink(destination:
                    Text("详情：\(item)").accessibilityIdentifier("detail_title")) {
                    Text(item).accessibilityIdentifier("row_\(item)")
                }
                .contextMenu {
                    Button("收藏") {}.accessibilityIdentifier("ctx_favorite")
                    Button("删除") {}.accessibilityIdentifier("ctx_delete")
                }
            }
        }
        .navigationTitle("列表")
    }
}

struct ToggleView: View {
    @State private var on = false

    var body: some View {
        Form {
            Toggle("推送通知", isOn: $on)
                .accessibilityIdentifier("push_toggle")
            Text(on ? "已开启" : "已关闭")
                .accessibilityIdentifier("toggle_state")
        }
        .navigationTitle("开关")
    }
}

@main
struct MatDemoApp: App {
    var body: some Scene {
        WindowGroup {
            NavigationView { HomeView() }
        }
    }
}
