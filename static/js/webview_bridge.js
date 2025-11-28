// webview_bridge.js
// Handles communication between the website and the Android/iOS WebView app

const WebViewBridge = {
    // List of Direct Link Ad URLs
    adUrls: [
        "https://otieu.com/4/10250311",
        "https://otieu.com/4/10205357",
        "https://otieu.com/4/9515888"
    ],

    // Cooldown in milliseconds (5 minutes)
    cooldown: 5 * 60 * 1000,

    /**
     * Trigger an ad via the native app bridge.
     * Selects a random URL and sends it to the app if not on cooldown.
     */
    triggerAd: function() {
        try {
            // Check cooldown
            const lastAd = sessionStorage.getItem('last_ad_time');
            const now = Date.now();
            if (lastAd && (now - parseInt(lastAd) < this.cooldown)) {
                console.log("Ad suppressed (cooldown active)");
                return;
            }

            // Pick random link
            const link = this.adUrls[Math.floor(Math.random() * this.adUrls.length)];

            // Send signal to Native App
            this.sendSignal(link);

            // Update timestamp
            sessionStorage.setItem('last_ad_time', now.toString());

        } catch (e) {
            console.error("Error triggering ad:", e);
        }
    },

    /**
     * Sends the URL to the native app using available bridges.
     * Supports Android (JavascriptInterface) and iOS (webkit.messageHandlers).
     */
    sendSignal: function(url) {
        let signalSent = false;

        // 1. Android Bridge
        // Assumes you create a JavascriptInterface named 'Android' with a method 'showAd(url)'
        if (window.Android && typeof window.Android.showAd === 'function') {
            console.log("Signaling Android Bridge:", url);
            window.Android.showAd(url);
            signalSent = true;
        }

        // 2. iOS Bridge
        // Assumes you set up a ScriptMessageHandler named 'showAd'
        if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.showAd) {
            console.log("Signaling iOS Bridge:", url);
            window.webkit.messageHandlers.showAd.postMessage(url);
            signalSent = true;
        }

        // 3. Fallback / Debug
        if (!signalSent) {
            console.log("Native Bridge not found. DEBUG - Show Ad:", url);
            // NOTE: In a real browser, this does nothing visible, which is what we want.
            // We do NOT want to open windows/tabs in the website itself.
        }
    }
};

// Expose globally
window.WebViewBridge = WebViewBridge;
