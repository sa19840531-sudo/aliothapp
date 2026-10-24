package com.alioth.observation;

import android.os.SystemClock;
import com.android.uiautomator.core.Configurator;
import com.android.uiautomator.core.UiDevice;
import com.android.uiautomator.testrunner.UiAutomatorTestCase;
import java.io.File;

/** 一次性读取当前结构；不执行输入，不把非空闲快照当作稳定证明。 */
public final class CurrentHierarchy extends UiAutomatorTestCase {
    public void testDump() {
        String output = getParams().getString("output");
        assertNotNull(output);
        assertTrue(output.matches("/data/local/tmp/alioth_uia_[0-9a-f]{32}\\.xml"));
        Configurator.getInstance().setWaitForIdleTimeout(0L);
        // 只给刚连接的可访问性桥准备时间，不等待应用事件流进入 idle。
        SystemClock.sleep(1000L);
        // legacy API 固定在数据目录下追加 local/tmp，因此只传本次文件名。
        UiDevice.getInstance().dumpWindowHierarchy(new File(output).getName());
        assertTrue(new File(output).length() > 0);
    }
}
