App({
  globalData: {
    token: '',
    openid: '',
    baseURL: 'https://link-jieniu.onrender.com',
  },

  onLaunch() {
    const token = wx.getStorageSync('token');
    if (token) {
      this.globalData.token = token;
    }
  },

  login() {
    return new Promise((resolve, reject) => {
      wx.login({
        success: (res) => {
          if (res.code) {
            wx.request({
              url: this.globalData.baseURL + '/api/login',
              method: 'POST',
              data: { code: res.code },
              success: (resp) => {
                if (resp.data && resp.data.token) {
                  this.globalData.token = resp.data.token;
                  wx.setStorageSync('token', resp.data.token);
                  resolve(resp.data);
                } else {
                  reject(new Error('登录失败'));
                }
              },
              fail: reject,
            });
          } else {
            reject(new Error('wx.login 失败'));
          }
        },
        fail: reject,
      });
    });
  },
});
