const imageUpload = document.getElementById('imageUpload');
const uploadedImage = document.getElementById('uploadedImage');
const confirmButton = document.getElementById('confirmButton');
const predictButton = document.getElementById('predictButton');
const resultDiv = document.getElementById('result');

let selectedFile;

document.getElementById('toggle-probabilities').addEventListener('click', function() {
    var container = document.getElementById('probability-container');
    if (container.style.display === 'none' || container.style.display === '') {
        container.style.display = 'block';
    } else {
        container.style.display = 'none';
    }
});

imageUpload.addEventListener('change', function(event) {
    const file = event.target.files[0];
    if (file) {
        const reader = new FileReader();
        reader.onload = function(e) {
            uploadedImage.src = e.target.result;
            uploadedImage.style.display = 'block';
            confirmButton.style.display = 'block';
            selectedFile = file;
        }
        reader.readAsDataURL(file);
    }
});

confirmButton.addEventListener('click', function() {
    confirmButton.style.display = 'none';
    predictButton.style.display = 'block';
    predictButton.disabled = false;
});

predictButton.addEventListener('click', function() {
    const formData = new FormData();
    formData.append('file', selectedFile);

    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/predict', true);
    xhr.onload = function() {
        if (xhr.status === 200) {
            const data = JSON.parse(xhr.responseText);
            resultDiv.innerHTML = `預測類別：${data.class} <br> 機率分布：${data.probabilities}`;
        } else {
            console.error('Error:', xhr.statusText);
        }
    };
    xhr.onerror = function() {
        console.error('Request failed');
    };
    xhr.send(formData);
});