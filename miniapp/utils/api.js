const app = getApp();

function request(method, path, data) {
  return new Promise((resolve, reject) => {
    const token = app.globalData.token || wx.getStorageSync('token') || '';
    wx.request({
      url: app.globalData.baseURL + path,
      method: method,
      data: data,
      header: {
        'Authorization': 'Bearer ' + token,
        'Content-Type': 'application/json',
      },
      success(res) {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          resolve(res.data);
        } else if (res.statusCode === 401) {
          // Token expired, re-login
          app.login().then(() => {
            request(method, path, data).then(resolve).catch(reject);
          }).catch(reject);
        } else {
          reject({ statusCode: res.statusCode, message: res.data?.detail || '请求失败' });
        }
      },
      fail(err) {
        reject({ message: '网络错误', error: err });
      },
    });
  });
}

module.exports = {
  getUser: () => request('GET', '/api/user'),
  createTask: (url) => request('POST', '/api/tasks', { url }),
  getTask: (taskId) => request('GET', '/api/tasks/' + taskId),
  listTasks: (page = 1) => request('GET', '/api/tasks?page=' + page),
  redeemCode: (code) => request('POST', '/api/codes/redeem', { code }),
};
